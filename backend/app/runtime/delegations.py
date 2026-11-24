"""Trusted local delegation administration; OPA judges a transaction-owned snapshot."""

import math
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from .database import RuntimeErrorCode
from .delegation_schema import links, receipts, dependencies, events, require
from .policy_evaluator import OpaEvaluator, validate
from .store import bounded, digest, identifier


async def lineage(c, scope, leaf):
    chain = []
    seen = set()
    while leaf is not None:
        if leaf in seen or len(chain) >= 4:
            raise RuntimeErrorCode("DELEGATION_CHAIN_INVALID")
        seen.add(leaf)
        row = (
            (
                await c.execute(
                    select(links).where(links.c.id == leaf, links.c.scope == scope)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeErrorCode("DELEGATION_NOT_FOUND")
        chain.append(dict(row))
        leaf = row["parent_ref"]
    return list(reversed(chain))


def receipt(chain, run_id, subject, actor, scope):
    return dict(
        run_id=run_id,
        scope=scope,
        subject_ref=subject,
        actor_ref=actor,
        chain=[x["id"] for x in chain],
        epochs={x["id"]: x["authorization_epoch"] for x in chain},
    )


async def evaluate(
    c, db, scope, reference, binding, chain, permission, config, evaluator=None
):
    from .delegation_identity import validate_chain

    await validate_chain(c, scope, chain)
    now = await db.now(c)
    if any(
        x["state"] != "active"
        or binding["epochs"].get(x["id"]) != x["authorization_epoch"]
        for x in chain
    ):
        raise RuntimeErrorCode("DELEGATION_REVOKED")
    if any(x["expires_at"] <= now for x in chain):
        raise RuntimeErrorCode("DELEGATION_EXPIRED")
    data = {
        x["id"]: {
            k: x[k]
            for k in (
                "id",
                "scope",
                "parent_ref",
                "subject_ref",
                "actor_ref",
                "audience",
                "permissions",
                "depth",
                "authorization_epoch",
                "state",
            )
        }
        | {
            "expires_at": datetime.fromtimestamp(
                x["expires_at"], timezone.utc
            ).isoformat()
        }
        for x in chain
    }
    context = dict(
        scope=scope,
        binding=dict(
            authorization_ref=reference,
            run_id=binding["run_id"],
            subject=binding["subject_ref"],
            actor=binding["actor_ref"],
        ),
        permission=permission,
        delegation=dict(links=data, work_bindings={reference: binding}),
    )
    result = await (evaluator or OpaEvaluator()).evaluate(config, {"context": context})
    if not result["allow"]:
        raise RuntimeErrorCode("DELEGATION_DENIED")
    await validate_chain(c, scope, chain)
    # Expiry can pass while OPA waits; the local authorization commit uses DB time.
    finished_at = await db.now(c)
    if any(x["expires_at"] <= finished_at for x in chain):
        raise RuntimeErrorCode("DELEGATION_EXPIRED")
    return result


async def revoke_record(c, db, scope, row, *, reason=None):
    """Same transaction fences a link and all dependent Work/Grant records."""
    from .work_auth_schema import work, grants

    ref = row["id"]
    changed = dict(
        row,
        state="revoked",
        version=row["version"] + 1,
        authorization_epoch=row["authorization_epoch"] + 1,
    )
    await c.execute(
        links.update()
        .where(links.c.id == ref)
        .values(
            state="revoked",
            version=changed["version"],
            authorization_epoch=changed["authorization_epoch"],
        )
    )
    affected = select(dependencies.c.authorization_ref).where(
        dependencies.c.scope == scope,
        dependencies.c.delegation_ref == ref,
    )
    result = await c.execute(
        work.update()
        .where(
            work.c.scope == scope,
            work.c.id.in_(affected),
            work.c.state == "active",
        )
        .values(state="revoked", version=work.c.version + 1)
    )
    await c.execute(
        grants.update()
        .where(grants.c.authorization_id.in_(affected), grants.c.state == "active")
        .values(state="revoked", version=grants.c.version + 1)
    )
    await c.execute(
        events.insert().values(
            id=str(uuid4()),
            scope=scope,
            delegation_ref=ref,
            operation="identity_revoked" if reason else "revoked",
            details={
                "version": changed["version"],
                "revoked_work_count": result.rowcount,
                **({"reason": reason} if reason else {}),
            },
            created_at=await db.now(c),
        )
    )
    return changed


class Delegations:
    def __init__(self, store, *, evaluator=None):
        self.store = store
        self.evaluator = evaluator

    async def issue(
        self,
        key,
        *,
        subject,
        actor,
        permissions,
        expires_at,
        policy_evaluator=None,
        parent_ref=None,
        issuer_actor=None,
        issuer_source=None,
    ):
        for value in (key, subject, actor):
            identifier(value)
        if parent_ref is not None:
            identifier(parent_ref)
            identifier(issuer_actor)
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at):
            raise RuntimeErrorCode("DELEGATION_EXPIRY_INVALID")
        if not isinstance(permissions, list) or not 1 <= len(permissions) <= 64:
            raise RuntimeErrorCode("DELEGATION_PERMISSIONS_INVALID")
        for p in permissions:
            if not isinstance(p, dict) or set(p) != {
                "action",
                "resource_kind",
                "resource_id",
                "resource_version",
                "constraint_revision",
            }:
                raise RuntimeErrorCode("DELEGATION_PERMISSIONS_INVALID")
            for v in p.values():
                identifier(v)
        request = dict(
            subject=subject,
            actor=actor,
            permissions=permissions,
            expires_at=expires_at,
            parent_ref=parent_ref,
            issuer_actor=issuer_actor,
            policy_evaluator=policy_evaluator,
        )
        if issuer_source is not None:
            request["issuer_source"] = issuer_source
        bounded(request)
        ref = "dl-" + digest([self.store.scope, key])[:60]
        fingerprint = digest(request)
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c)
            if issuer_source is not None:
                from .delegation_identity import validate_source

                if (
                    not parent_ref
                    or issuer_actor != issuer_source["principal"]["actor"]
                ):
                    raise RuntimeErrorCode("AUTH_ACTION_DENIED")
                await validate_source(
                    c, self.store.scope, issuer_source, permissions, expires_at
                )
            now = await self.store.db.now(c)
            old = (
                (
                    await c.execute(
                        select(links).where(
                            links.c.id == ref, links.c.scope == self.store.scope
                        )
                    )
                )
                .mappings()
                .first()
            )
            if old:
                if old["request_digest"] != fingerprint:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return self.public(old)
            chain = await lineage(c, self.store.scope, parent_ref) if parent_ref else []
            if chain and (
                chain[-1]["actor_ref"] != issuer_actor
                or chain[-1]["subject_ref"] != subject
            ):
                raise RuntimeErrorCode("DELEGATION_ISSUER_DENIED")
            if len(chain) >= 4:
                raise RuntimeErrorCode("DELEGATION_DEPTH_EXCEEDED")
            if not chain and not isinstance(policy_evaluator, dict):
                raise RuntimeErrorCode("POLICY_EVALUATOR_CONFIGURATION_INVALID")
            config = (
                chain[-1]["policy_evaluator"] if chain else validate(policy_evaluator)
            )
            if chain and policy_evaluator is not None:
                raise RuntimeErrorCode("DELEGATION_POLICY_INHERITED")
            row = dict(
                id=ref,
                scope=self.store.scope,
                request_digest=fingerprint,
                parent_ref=parent_ref,
                subject_ref=subject,
                actor_ref=actor,
                audience="dios-runtime",
                permissions=permissions,
                expires_at=expires_at,
                depth=len(chain),
                authorization_epoch=1,
                state="active",
                version=1,
                policy_evaluator=config,
                created_at=now,
            )
            chain.append(row)
            binding = receipt(chain, ref, subject, actor, self.store.scope)
            decision = await evaluate(
                c,
                self.store.db,
                self.store.scope,
                ref,
                binding,
                chain,
                permissions[0],
                config,
                self.evaluator,
            )
            if issuer_source is not None:
                await validate_source(
                    c, self.store.scope, issuer_source, permissions, expires_at
                )
            await c.execute(links.insert().values(**row))
            if issuer_source is not None:
                from .delegation_identity import bind_issuer

                await bind_issuer(c, self.store.scope, ref, issuer_source)
            await c.execute(
                events.insert().values(
                    id=str(uuid4()),
                    scope=self.store.scope,
                    delegation_ref=ref,
                    operation="issued",
                    details={
                        "parent_ref": parent_ref,
                        "issuer_actor": issuer_actor,
                        **(
                            {
                                "authenticated_issuer": {
                                    k: issuer_source["principal"][k]
                                    for k in (
                                        "issuer",
                                        "id",
                                        "scope",
                                        "subject",
                                        "actor",
                                        "revision",
                                    )
                                }
                            }
                            if issuer_source is not None
                            else {}
                        ),
                        "decision": decision,
                    },
                    created_at=await self.store.db.now(c),
                )
            )
            return self.public(row)

    async def revoke(self, ref, version, *, access_guard=None):
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c)
            chain = await lineage(c, self.store.scope, ref)
            if access_guard is not None:
                await access_guard(c, chain)
            row = chain[-1]
            if row["state"] == "revoked":
                return self.public(row)
            if row["version"] != version:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            return self.public(
                await revoke_record(c, self.store.db, self.store.scope, row)
            )

    async def show(self, ref, *, access_guard=None):
        async with self.store.db.read(self.store.scope) as c:
            await require(c)
            chain = await lineage(c, self.store.scope, ref)
            if access_guard is not None:
                await access_guard(c, chain)
            now = await self.store.db.now(c)
            history = (
                (
                    await c.execute(
                        select(events)
                        .where(
                            events.c.scope == self.store.scope,
                            events.c.delegation_ref == ref,
                        )
                        .order_by(events.c.created_at)
                    )
                )
                .mappings()
                .all()
            )
            from .delegation_reconciliation import observation

            return {
                "source_check": await observation(c, self.store.scope, ref, now),
                "chain": [self.public(x) for x in chain],
                "effective": all(
                    x["state"] == "active" and x["expires_at"] > now for x in chain
                ),
                "events": [
                    dict(
                        id=x["id"],
                        operation=x["operation"],
                        created_at=x["created_at"],
                        details=x["details"],
                    )
                    for x in history
                ],
            }

    @staticmethod
    def public(row):
        return {
            k: v
            for k, v in row.items()
            if k not in {"request_digest", "policy_evaluator"}
        }


async def bind(c, db, scope, run, descriptor, expires_at):
    ref = descriptor.get("delegation_ref")
    if not ref:
        return expires_at
    await require(c)
    chain = await lineage(c, scope, ref)
    binding = receipt(
        chain, run["id"], descriptor["subject"], descriptor["actor"], scope
    )
    reference = run["input"]["work_authorization_ref"]
    from .work_policy import permission

    for action in ("execute", "accept_result"):
        await evaluate(
            c,
            db,
            scope,
            reference,
            binding,
            chain,
            permission(descriptor, action),
            chain[-1]["policy_evaluator"],
        )
    await c.execute(
        receipts.insert().values(
            authorization_ref=reference,
            scope=scope,
            run_id=run["id"],
            leaf_ref=ref,
            binding=binding,
            policy_evaluator=chain[-1]["policy_evaluator"],
            created_at=await db.now(c),
        )
    )
    for row in chain:
        await c.execute(
            dependencies.insert().values(
                authorization_ref=reference, delegation_ref=row["id"], scope=scope
            )
        )
    return min(expires_at, *(x["expires_at"] for x in chain))


async def check_work(c, db, run, authorization, permission):
    ref = run["input"].get("delegation_ref")
    if not ref:
        return
    await require(c)
    row = (
        (
            await c.execute(
                select(receipts).where(
                    receipts.c.authorization_ref == authorization["id"],
                    receipts.c.scope == run["scope"],
                    receipts.c.run_id == run["id"],
                )
            )
        )
        .mappings()
        .first()
    )
    if not row or row["leaf_ref"] != ref:
        raise RuntimeErrorCode("DELEGATION_BINDING_MISMATCH")
    chain = await lineage(c, run["scope"], ref)
    if row["binding"] != receipt(
        chain, run["id"], authorization["subject"], authorization["actor"], run["scope"]
    ):
        raise RuntimeErrorCode("DELEGATION_BINDING_MISMATCH")
    await evaluate(
        c,
        db,
        run["scope"],
        authorization["id"],
        row["binding"],
        chain,
        permission,
        row["policy_evaluator"],
    )


async def describe_work(c, run):
    if not run["input"].get("delegation_ref"):
        return None
    await require(c)
    saved = (
        (
            await c.execute(
                select(receipts).where(
                    receipts.c.authorization_ref
                    == run["input"]["work_authorization_ref"],
                    receipts.c.scope == run["scope"],
                    receipts.c.run_id == run["id"],
                )
            )
        )
        .mappings()
        .first()
    )
    if saved is None:
        raise RuntimeErrorCode("DELEGATION_BINDING_MISMATCH")
    chain = await lineage(c, run["scope"], saved["leaf_ref"])
    from .delegation_reconciliation import observation
    from .database import Database

    now = await Database.now(c)
    return {
        "reference": saved["leaf_ref"],
        "captured_at": saved["created_at"],
        "chain": [
            dict(
                id=x["id"],
                parent_ref=x["parent_ref"],
                subject=x["subject_ref"],
                actor=x["actor_ref"],
                depth=x["depth"],
                state=x["state"],
                expires_at=x["expires_at"],
                authorization_epoch=x["authorization_epoch"],
                captured_epoch=saved["binding"]["epochs"][x["id"]],
                source_check=await observation(c, run["scope"], x["id"], now),
            )
            for x in chain
        ],
    }
