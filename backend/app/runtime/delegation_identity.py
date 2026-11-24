"""Reuse authenticated principals and OIDC logout fences for delegation issuers."""

import asyncio
from sqlalchemy import select
from .database import RuntimeErrorCode
from .delegation_identity_schema import issuers, check
from .principals import FilePrincipals
from .logout_events import require_source


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
        try:
            await validate_source(
                c, scope, source, link["permissions"], link["expires_at"]
            )
        except RuntimeErrorCode as exc:
            if exc.code in {"AUTH_DELEGATION_DENIED", "AUTH_DELEGATION_DEADLINE"}:
                # Existing Work uses the same persistent revocation handling as
                # a withdrawn submitter identity. Issuance retains specific errors.
                raise RuntimeErrorCode("AUTH_PRINCIPAL_REVOKED") from None
            raise


async def bind_issuer(c, scope, ref, source):
    if not await check(c):
        raise RuntimeErrorCode("DELEGATION_IDENTITY_MIGRATION_REQUIRED")
    await c.execute(issuers.insert().values(delegation_ref=ref, scope=scope, **source))
