"""Reuse authenticated principals and OIDC logout fences for delegation issuers."""

import asyncio
from sqlalchemy import select
from .database import Database, DurableRejection, RuntimeErrorCode
from .delegation_identity_schema import issuers, check
from .principals import FilePrincipals
from .logout_events import require_source


class SourceRevoked(DurableRejection):
    def __init__(self, scope, link, reason):
        super().__init__("AUTH_PRINCIPAL_REVOKED")
        self.scope, self.ref, self.version = scope, link["id"], link["version"]
        self.reason = reason

    async def persist(self, c):
        from .delegation_schema import links
        from .delegations import revoke_record

        row = (
            (
                await c.execute(
                    select(links).where(
                        links.c.scope == self.scope, links.c.id == self.ref
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is not None and row["state"] == "active":
            if row["version"] != self.version:
                raise RuntimeErrorCode("DELEGATION_BINDING_MISMATCH")
            await revoke_record(c, Database, self.scope, row, reason=self.reason)

    async def persist_after_rollback(self, db, scope):
        if scope != self.scope:
            raise RuntimeErrorCode("DELEGATION_BINDING_MISMATCH")
        async with db.transaction(scope) as c:
            await self.persist(c)


async def validate_source(c, scope, source, permissions, expires_at):
    binding = source["principal"]
    if binding["scope"] != scope:
        raise RuntimeErrorCode("AUTH_ACTION_DENIED")
    await require_source(c, scope, binding)
    return await asyncio.to_thread(current_source, source, permissions, expires_at)


def current_source(source, permissions, expires_at):
    row = FilePrincipals(source["provider_path"]).current(source["principal"])
    grant = row["profiles"].get(source["profile_id"], {})
    if "delegation.issue" not in grant.get("actions", []) or any(
        p not in grant.get("permissions", []) for p in permissions
    ):
        raise RuntimeErrorCode("AUTH_DELEGATION_DENIED")
    if expires_at > row["expires_at"]:
        raise RuntimeErrorCode("AUTH_DELEGATION_DEADLINE")
    return row


async def validate_chain(c, scope, chain):
    if not await check(c):
        return  # Existing CLI-only databases remain readable.
    records = (
        (
            await c.execute(
                select(issuers).where(
                    issuers.c.scope == scope,
                    issuers.c.delegation_ref.in_([x["id"] for x in chain]),
                )
            )
        )
        .mappings()
        .all()
    )
    by_id = {x["id"]: x for x in chain}
    for source in records:
        link = by_id[source["delegation_ref"]]
        if link["state"] != "active":
            raise RuntimeErrorCode("DELEGATION_REVOKED")
        try:
            await validate_source(
                c, scope, source, link["permissions"], link["expires_at"]
            )
        except RuntimeErrorCode as exc:
            if exc.code in {
                "AUTH_DELEGATION_DENIED",
                "AUTH_DELEGATION_DEADLINE",
                "AUTH_PRINCIPAL_REVOKED",
                "AUTH_PRINCIPAL_EXPIRED",
            }:
                rejection = SourceRevoked(scope, link, exc.code)
                # Callers that record a denial and commit retain the observation
                # here. Uncaught rejection persists it after operation rollback.
                await rejection.persist(c)
                raise rejection from None
            raise


async def bind_issuer(c, scope, ref, source):
    if not await check(c):
        raise RuntimeErrorCode("DELEGATION_IDENTITY_MIGRATION_REQUIRED")
    await c.execute(issuers.insert().values(delegation_ref=ref, scope=scope, **source))


async def revocation_evidence(c, scope, chain):
    """Expose committed source tombstones and local link revocations together."""
    from .logout_events import observation
    from .delegation_schema import events

    refs = [link["id"] for link in chain]
    result = {ref: {"source_event": None, "revocation": None} for ref in refs}
    if await check(c):
        sources = (
            (
                await c.execute(
                    select(issuers).where(
                        issuers.c.scope == scope, issuers.c.delegation_ref.in_(refs)
                    )
                )
            )
            .mappings()
            .all()
        )
        for source in sources:
            result[source["delegation_ref"]]["source_event"] = await observation(
                c, scope, source["principal"]
            )
    # Each link has one terminal transition. Read its most recent audit event
    # without treating an execution cancellation request as a stop receipt.
    for ref in refs:
        event = (
            (
                await c.execute(
                    select(events)
                    .where(
                        events.c.scope == scope,
                        events.c.delegation_ref == ref,
                        events.c.operation.in_(("revoked", "identity_revoked")),
                    )
                    .order_by(events.c.created_at.desc(), events.c.id)
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if event:
            result[ref]["revocation"] = {
                key: event[key] for key in ("id", "operation", "created_at", "details")
            }
    return result
