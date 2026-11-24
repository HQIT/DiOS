"""Verified events fence present and future Work within the existing scope transaction."""

from sqlalchemy import select, and_, or_, update
from .database import RuntimeErrorCode
from .store import digest
from .schema import runs
from .work_auth_schema import work, grants
from .logout_schema import events, check


async def observation(c, scope, binding):
    source = (binding or {}).get("upstream") or {}
    if not source or not await check(c):
        return
    sid = source.get("session_id", "")
    session = and_(
        events.c.session_id != "",
        events.c.session_id == sid,
        or_(events.c.subject == "", events.c.subject == source.get("subject", "")),
    )
    subject = and_(
        events.c.session_id == "", events.c.subject == source.get("subject", "")
    )
    if "session_started_at" in source:
        subject = and_(subject, events.c.issued_at >= source["session_started_at"])
    found = (
        (
            await c.execute(
                select(
                    events.c.id,
                    events.c.issued_at,
                    events.c.received_at,
                    events.c.revoked_work_count,
                )
                .where(
                    events.c.scope == scope,
                    events.c.issuer == source.get("issuer"),
                    events.c.client_id == source.get("client_id"),
                    or_(session, subject),
                )
                .order_by(events.c.received_at.desc(), events.c.id)
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    return dict(found) if found else None


async def require_source(c, scope, binding):
    if await observation(c, scope, binding):
        raise RuntimeErrorCode("AUTH_PRINCIPAL_REVOKED")


async def receive(store, event, token_digest):
    identity = digest(
        [store.scope, event["issuer"], event["client_id"], event["event_id"]]
    )
    content = digest(event)
    async with store.db.transaction(store.scope) as c:
        if not await check(c):
            raise RuntimeErrorCode("LOGOUT_MIGRATION_REQUIRED")
        prior = (
            (await c.execute(select(events).where(events.c.id == identity)))
            .mappings()
            .first()
        )
        if prior:
            if prior["digest"] != content:
                raise RuntimeErrorCode("LOGOUT_EVENT_CONFLICT")
            return {
                "duplicate": True,
                "event_id": identity,
                "revoked_work_count": prior["revoked_work_count"],
            }
        now = await store.db.now(c)
        source = runs.c.input["principal"]["upstream"]
        conditions = [
            runs.c.scope == store.scope,
            source["issuer"].as_string() == event["issuer"],
            source["client_id"].as_string() == event["client_id"],
        ]
        if event["session_id"]:
            conditions.append(source["session_id"].as_string() == event["session_id"])
            if event["subject"]:
                conditions.append(source["subject"].as_string() == event["subject"])
        else:
            conditions.append(source["subject"].as_string() == event["subject"])
            conditions.append(
                or_(
                    source["session_started_at"].as_float().is_(None),
                    source["session_started_at"].as_float() <= event["issued_at"],
                )
            )
        references = (
            select(work.c.id)
            .join(runs, work.c.run_id == runs.c.id)
            .where(work.c.scope == store.scope, *conditions)
        )
        result = await c.execute(
            update(work)
            .where(work.c.id.in_(references), work.c.state == "active")
            .values(state="revoked", version=work.c.version + 1)
        )
        await c.execute(
            update(grants)
            .where(
                grants.c.authorization_id.in_(references), grants.c.state == "active"
            )
            .values(state="revoked", version=grants.c.version + 1)
        )
        await c.execute(
            events.insert().values(
                id=identity,
                scope=store.scope,
                **event,
                received_at=now,
                digest=content,
                token_digest=token_digest,
                revoked_work_count=result.rowcount,
            )
        )
        return {
            "duplicate": False,
            "event_id": identity,
            "revoked_work_count": result.rowcount,
        }
