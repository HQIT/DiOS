"""Verified events fence present and future Work within the existing scope transaction."""

from sqlalchemy import select, and_, or_, update, union, func
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
                    func.min(events.c.received_at).over().label("first_received_at"),
                    func.max(events.c.received_at).over().label("last_received_at"),
                    func.count().over().label("event_count"),
                )
                .where(
                    events.c.scope == scope,
                    events.c.issuer == source.get("issuer"),
                    events.c.client_id == source.get("client_id"),
                    or_(session, subject),
                )
                # Receipt order is not source order. A delayed older event must
                # not move the observed source-time watermark backwards.
                .order_by(events.c.issued_at.desc(), events.c.received_at, events.c.id)
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


def source_matches(source, event):
    conditions = [source['issuer'].as_string() == event['issuer'],
                  source['client_id'].as_string() == event['client_id']]
    if event['session_id']:
        conditions.append(source['session_id'].as_string() == event['session_id'])
        if event['subject']:
            conditions.append(source['subject'].as_string() == event['subject'])
    else:
        conditions.extend([source['subject'].as_string() == event['subject'],
            or_(source['session_started_at'].as_float().is_(None),
                source['session_started_at'].as_float() <= event['issued_at'])])
    return and_(*conditions)


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
        references = (
            select(work.c.id)
            .join(runs, work.c.run_id == runs.c.id)
            .where(work.c.scope == store.scope, runs.c.scope == store.scope,
                   source_matches(runs.c.input['principal']['upstream'], event))
        )
        from .delegation_identity_schema import issuers, check as identity_check
        if await identity_check(c):
            from .delegation_schema import links, dependencies, events as delegation_events
            affected = select(issuers.c.delegation_ref).where(issuers.c.scope == store.scope,
                source_matches(issuers.c.principal['upstream'], event))
            references = union(references, select(dependencies.c.authorization_ref).where(
                dependencies.c.scope == store.scope, dependencies.c.delegation_ref.in_(affected)))
            revoked = (await c.execute(select(links.c.id).where(links.c.scope == store.scope,
                links.c.id.in_(affected), links.c.state == 'active'))).scalars().all()
            await c.execute(links.update().where(links.c.scope == store.scope,
                links.c.id.in_(affected), links.c.state == 'active').values(
                    state='revoked', version=links.c.version+1, authorization_epoch=links.c.authorization_epoch+1))
            # One immutable audit event per affected link, tied to the verified source event.
            for ref in revoked:
                await c.execute(delegation_events.insert().values(
                    id=digest([identity, ref]), scope=store.scope, delegation_ref=ref,
                    operation='identity_revoked', details={'logout_event_id':identity}, created_at=now))
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
