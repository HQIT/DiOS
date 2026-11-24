"""Authenticated derivation of existing authority; root issuance stays with operators."""

import asyncio
from app.runtime.database import RuntimeErrorCode
from app.runtime.delegations import Delegations, lineage
from app.runtime.principals import BINDING_KEYS
from app.runtime.delegation_identity_schema import issuers, check
from app.runtime.logout_events import require_source
from app.runtime.store import digest
from sqlalchemy import select


class AuthorizedDelegations:
    def __init__(self, console):
        self.console = console
        if console.principal is None:
            raise RuntimeErrorCode("AUTH_PRINCIPAL_REQUIRED")
        self.manager = Delegations(console.store)

    async def issue(self, body):
        provider = await asyncio.to_thread(self.console.identity_provider)
        if provider is None:
            raise RuntimeErrorCode("AUTH_PRINCIPAL_REVOKED")
        principal = self.console.principal
        async with self.console.db.read(self.console.scope) as c:
            chain = await lineage(c, self.console.scope, body["parent_ref"])
        parent = chain[-1]
        if parent["actor_ref"] != principal["actor"]:
            raise RuntimeErrorCode("AUTH_ACTION_DENIED")
        source = dict(
            provider_path=str(provider.path),
            principal=principal,
            profile_id=body["profile_id"],
        )
        # Identity and request namespaces are independent of caller-selected keys.
        key = (
            "api-"
            + digest([*[principal[k] for k in BINDING_KEYS], body["idempotency_key"]])[
                :60
            ]
        )
        return await self.manager.issue(
            key,
            subject=parent["subject_ref"],
            actor=body["actor"],
            permissions=body["permissions"],
            expires_at=body["expires_at"],
            parent_ref=parent["id"],
            issuer_actor=principal["actor"],
            issuer_source=source,
        )

    async def guard(self, c, chain, action):
        principal = self.console.principal
        await require_source(c, self.console.scope, principal)
        row = await asyncio.to_thread(self.console.access_row)
        if not await check(c):
            raise RuntimeErrorCode("DELEGATION_IDENTITY_MIGRATION_REQUIRED")
        source = (
            (
                await c.execute(
                    select(issuers).where(
                        issuers.c.delegation_ref == chain[-1]["id"],
                        issuers.c.scope == self.console.scope,
                    )
                )
            )
            .mappings()
            .first()
        )
        # Only the authenticated issuer can manage an API-issued delegation.
        if source is None or any(
            source["principal"][k] != principal[k] for k in BINDING_KEYS
        ):
            raise RuntimeErrorCode("NOT_FOUND")
        if action not in row["profiles"].get(source["profile_id"], {}).get(
            "actions", []
        ):
            raise RuntimeErrorCode("AUTH_ACTION_DENIED")

    async def show(self, ref):
        return await self.manager.show(
            ref, access_guard=lambda c, chain: self.guard(c, chain, "delegation.read")
        )

    async def revoke(self, ref, version):
        return await self.manager.revoke(
            ref,
            version,
            access_guard=lambda c, chain: self.guard(c, chain, "delegation.revoke"),
        )
