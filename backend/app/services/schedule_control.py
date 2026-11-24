"""Inspect durable timers and resolve pre-dispatch dead letters."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select

from app.config import settings
from app.models.tables import A2ATask, CronCursor, CronOccurrence, EventLog
from app.runtime.database import RuntimeErrorCode
from app.services.durable_cron import ownership, refresh, utc
from app.services.e2ag import append_audit_entry, verify_audit_chain


def view(item):
    return {
        key: utc(value).isoformat() if isinstance(value, datetime) else value
        for key in (
            "id",
            "subscription_id",
            "revision",
            "first_due",
            "last_due",
            "trigger_count",
            "event_id",
            "state",
            "attempts",
            "next_retry_at",
            "error",
            "resolution",
            "updated_at",
        )
        for value in (getattr(item, key),)
    }


async def summary(db, subscription_id):
    cursor = await db.get(CronCursor, subscription_id)
    if not cursor or cursor.scope != settings.runtime_scope:
        return None
    active = (
        await db.get(CronOccurrence, cursor.active_id) if cursor.active_id else None
    )
    buffered = (
        await db.get(CronOccurrence, cursor.buffered_id) if cursor.buffered_id else None
    )
    return {
        "subscription_id": subscription_id,
        "revision": cursor.revision,
        "enabled": bool(cursor.configuration.get("enabled") and cursor.next_due),
        "timezone": "UTC",
        "overlap": "buffer_one",
        "catchup_seconds": 86400,
        "next_due": utc(cursor.next_due).isoformat() if cursor.next_due else None,
        "observed_at": utc(cursor.updated_at).isoformat(),
        "error": cursor.error,
        "active": view(active) if active else None,
        "buffered": view(buffered) if buffered else None,
    }


async def list_schedules(db):
    ids = (
        await db.scalars(
            select(CronCursor.subscription_id)
            .where(CronCursor.scope == settings.runtime_scope)
            .order_by(CronCursor.subscription_id)
        )
    ).all()
    return [await summary(db, identity) for identity in ids]


async def history(db, subscription_id, *, limit=50, before=None):
    if not 1 <= limit <= 100:
        raise RuntimeErrorCode("INVALID_LIMIT")
    query = select(CronOccurrence).where(
        CronOccurrence.scope == settings.runtime_scope,
        CronOccurrence.subscription_id == subscription_id,
    )
    # First due is immutable even when buffered ranges are extended.
    if before:
        cursor = await db.get(CronOccurrence, before)
        if (
            not cursor
            or cursor.scope != settings.runtime_scope
            or cursor.subscription_id != subscription_id
        ):
            raise RuntimeErrorCode("INVALID_CURSOR")
        query = query.where(
            or_(
                CronOccurrence.first_due < cursor.first_due,
                and_(
                    CronOccurrence.first_due == cursor.first_due,
                    CronOccurrence.id < before,
                ),
            )
        )
    rows = (
        await db.scalars(
            query.order_by(
                CronOccurrence.first_due.desc(), CronOccurrence.id.desc()
            ).limit(limit)
        )
    ).all()
    return {
        "items": [view(row) for row in rows],
        "next_cursor": rows[-1].id if len(rows) == limit else None,
    }


async def resolve(identity, *, action, actor, reason, idempotency_key):
    if action not in {"retry", "discard"} or not all(
        isinstance(v, str) and v.strip() and len(v) <= size
        for v, size in ((actor, 128), (reason, 2000), (idempotency_key, 128))
    ):
        raise RuntimeErrorCode("INVALID_RESOLUTION")
    from app.db.database import async_session

    async with ownership(), async_session() as db:
        item = await db.get(CronOccurrence, identity)
        if not item or item.scope != settings.runtime_scope:
            raise RuntimeErrorCode("NOT_FOUND")
        request = {
            "action": action,
            "actor": actor,
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        entries = (item.resolution or {}).get("entries", [])
        for entry in entries:
            if entry["idempotency_key"] == idempotency_key:
                if any(entry[k] != v for k, v in request.items()):
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return view(item)
        await refresh(db, item)
        if item.state != "dead_letter":
            raise RuntimeErrorCode("SCHEDULE_NOT_DEAD_LETTER")
        log = await db.get(EventLog, item.event_id)
        tasks = list(
            (
                await db.scalars(
                    select(A2ATask).where(A2ATask.context_id == item.event_id)
                )
            ).all()
        )
        if any(
            t.status not in {"completed", "failed", "canceled", "rejected"}
            for t in tasks
        ):
            raise RuntimeErrorCode("SCHEDULE_ORIGINAL_TASK_ACTIVE")
        if action == "retry" and (log or tasks):
            raise RuntimeErrorCode("SCHEDULE_ORIGINAL_DELIVERY_REQUIRES_RECONCILIATION")
        now = datetime.now(UTC)
        entry = {
            **request,
            "at": now.isoformat(),
            "previous_attempts": item.attempts,
            "previous_error": item.error,
        }
        item.resolution = {"entries": [*entries, entry]}
        item.state = "pending" if action == "retry" else "discarded"
        item.attempts, item.next_retry_at, item.updated_at = 0, None, now
        if log:
            if not verify_audit_chain(log.audit_chain or []):
                raise RuntimeErrorCode("SCHEDULE_AUDIT_INVALID")
            log.audit_chain = append_audit_entry(
                log.audit_chain or [],
                trace_id=log.trace_id,
                stage="schedule_resolution",
                outcome=action,
                evidence=entry,
            )
        await db.commit()
        return view(item)


async def main(args):
    if args.command == "resolve":
        return await resolve(
            args.occurrence_id,
            action=args.action,
            actor=args.actor,
            reason=args.reason,
            idempotency_key=args.idempotency_key,
        )
    from app.db.database import async_session

    async with async_session() as db:
        if args.command == "list":
            return await list_schedules(db)
        return await history(
            db, args.subscription_id, limit=args.limit, before=args.before
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    h = commands.add_parser("history")
    h.add_argument("subscription_id")
    h.add_argument("--limit", type=int, default=50)
    h.add_argument("--before")
    r = commands.add_parser("resolve")
    r.add_argument("occurrence_id")
    r.add_argument("--action", choices=["retry", "discard"], required=True)
    for key in ("actor", "reason", "idempotency-key"):
        r.add_argument("--" + key, required=True)
    try:
        print(json.dumps(asyncio.run(main(parser.parse_args())), ensure_ascii=False))
    except RuntimeErrorCode as exc:
        print(json.dumps({"error": exc.code}))
        raise SystemExit(2) from None
