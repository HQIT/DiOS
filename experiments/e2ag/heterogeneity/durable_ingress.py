"""Experimental durable handoff, common to file/broker consumers.

One configured consumer owns a source. recover_interrupted is allowed ONLY
after the previous consumer is confirmed stopped, not after a lease timeout.
The journal is additional adapter infrastructure, not a replacement PDP.
"""
import hashlib
import json

from sqlalchemy import Column, MetaData, String, Table, select, update

metadata = MetaData()
receipts = Table("hetero_receipts", metadata,
    Column("identity", String(64), primary_key=True),
    Column("source", String(256), nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("digest", String(64), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("event_log_id", String(12), nullable=False, default=""))
conflicts = Table("hetero_conflicts", metadata,
    Column("key", String(64), primary_key=True),
    Column("identity", String(64), nullable=False),
    Column("expected_digest", String(64), nullable=False),
    Column("observed_digest", String(64), nullable=False))


def fingerprint(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()


def insert_once(db, table, values, index):
    dialect = db.bind.dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise RuntimeError(f"Unsupported journal dialect: {dialect}")
    return insert(table).values(**values).on_conflict_do_nothing(index_elements=index)


async def accept_event(event, db, *, recover_interrupted=False, checkpoint=lambda _: None):
    from app.models.tables import EventLog, Subscription, E2AGApproval
    from app.services.event_router import match_subscriptions
    from app.services.event_dispatcher import dispatch_event
    from app.services.event_normalizer import compute_dedup_hash
    from app.services.e2ag import verify_audit_chain

    identity = fingerprint([event["source"], event["id"]])
    digest = event["deliverydigest"]
    await db.execute(insert_once(db, receipts, dict(identity=identity, source=event["source"],
        event_id=event["id"], digest=digest, phase="received", event_log_id=""), ["identity"]))
    checkpoint("before_receive_commit")
    await db.commit()
    checkpoint("after_receive_commit")
    row = (await db.execute(select(receipts).where(receipts.c.identity == identity))).mappings().one()
    if row["digest"] != digest:
        await db.execute(insert_once(db, conflicts, dict(key=fingerprint([identity, digest]),
            identity=identity, expected_digest=row["digest"], observed_digest=digest), ["key"]))
        await db.commit()
        return dict(disposition="conflict", acknowledge=True, event_log_id=row["event_log_id"])
    if row["phase"] == "recorded":
        return dict(disposition="duplicate", acknowledge=True, event_log_id=row["event_log_id"])
    if row["phase"] == "dispatching" and not recover_interrupted:
        return dict(disposition="in_progress", acknowledge=False, event_log_id="")

    # Recovery searches the native log, not a guessed execution result. The
    # adapter identity is retained beyond the dispatcher's short dedup window.
    logs = (await db.execute(select(EventLog).where(
        EventLog.dedup_hash == compute_dedup_hash(event)))).scalars().all()
    if len(logs) > 1:
        return dict(disposition="ambiguous_logs", acknowledge=False, event_log_id="")
    log = logs[0] if logs else None
    if log is not None and log.cloud_event.get("deliverydigest") != digest:
        return dict(disposition="log_content_conflict", acknowledge=False, event_log_id=log.id)
    if log is None:
        if row["phase"] == "received":
            claim = await db.execute(update(receipts).where(receipts.c.identity == identity,
                receipts.c.phase == "received").values(phase="dispatching"))
            await db.commit()
            if claim.rowcount != 1:
                return dict(disposition="in_progress", acknowledge=False, event_log_id="")
        checkpoint("before_dispatch")
        subscriptions = (await db.execute(select(Subscription).where(Subscription.enabled == True))).scalars().all()
        log, error = await dispatch_event(event, match_subscriptions(event, subscriptions), db)
        checkpoint("after_dispatch_commit")
        if log is None:
            return dict(disposition="dispatch_unresolved", acknowledge=False, event_log_id="", detail=error)
    # A crash between native intake and final dispatch is not a safe reason to
    # replay a task. Keep it visible rather than silently drop or duplicate it.
    valid = verify_audit_chain(log.audit_chain)
    completed_handoff = log.status in {"dispatched", "denied", "failed",
        "approval_rejected", "approval_expired"}
    if log.status == "approval_required":
        approval = (await db.execute(select(E2AGApproval).where(E2AGApproval.event_log_id == log.id))).scalar_one_or_none()
        completed_handoff = approval is not None
    if not valid or not completed_handoff:
        return dict(disposition="native_state_unresolved", acknowledge=False, event_log_id=log.id,
                    native_status=log.status)
    await db.execute(update(receipts).where(receipts.c.identity == identity).values(
        phase="recorded", event_log_id=log.id))
    await db.commit()
    checkpoint("after_handoff_commit")
    return dict(disposition="recorded", acknowledge=True, event_log_id=log.id, native_status=log.status)
