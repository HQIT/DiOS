"""Authenticated derivation of existing authority; root issuance stays with operators."""

import asyncio
from app.runtime.database import RuntimeErrorCode
from app.runtime.delegations import Delegations, lineage
from app.runtime.principals import BINDING_KEYS
from app.runtime.delegation_identity_schema import issuers, check
from app.runtime.logout_events import require_source
from app.runtime.store import digest
from sqlalchemy import select


def submission_guard(console, descriptor, reference, previous=None):
    """An explicit leaf can narrow, but cannot replace, a configured ancestor."""
    if descriptor is None:
        raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
    anchor = descriptor.get("delegation_ref")

    async def guard(c):
        chain = await lineage(c, console.scope, reference)
        if anchor is not None and anchor not in {row["id"] for row in chain}:
            raise RuntimeErrorCode("DELEGATION_ANCESTOR_REQUIRED")
        leaf = chain[-1]
        if (leaf["subject_ref"] != console.principal["subject"]
                or leaf["actor_ref"] != console.principal["actor"]):
            raise RuntimeErrorCode("AUTH_DELEGATION_BINDING_MISMATCH")
        if previous is not None:
            await previous(c)
        # Runtime.create binds the leaf, checks its live chain with OPA, clamps
        # the deadline and persists dependencies in this same admission txn.

    return guard


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
        handoff=None;guard=None
        if body.get('parent_run_id') is not None:
            from .child_recipients import prepare
            config=await asyncio.to_thread(self.console.policy.configuration)
            target=config['profiles'].get(body['profile_id'])
            if target is None:raise RuntimeErrorCode('AUTH_PROFILE_DENIED')
            binding,guard=await prepare(self.console,body['parent_run_id'],body['profile_id'],
                target['profile_digest'],body['parent_ref'])
            handoff=dict(parent_execution=binding,profile_id=body['profile_id'],profile_digest=target['profile_digest'])
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
            handoff=handoff,issuance_guard=guard,
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
