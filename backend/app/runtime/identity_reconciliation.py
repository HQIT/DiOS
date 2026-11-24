"""Bounded source reconciliation using existing Providers and fenced DB leases."""

import asyncio
import math
from uuid import uuid4

from sqlalchemy import and_, func, or_, select

from app.services.e2ag import Decision

from .database import RuntimeErrorCode
from .identity_check_schema import check, checks, require
from .schema import runs
from .store import TERMINAL, digest
from .work_auth_schema import work
from .work_authorizations import audit, persist_identity_revocation

PERMANENT = {"AUTH_PRINCIPAL_REVOKED", "AUTH_PRINCIPAL_EXPIRED"}


class IdentityReconciler:
    def __init__(self, store, policy, *, interval=60, retry_base=15, retry_max=300):
        for value in (interval, retry_base, retry_max):
            if (
                type(value) not in (float, int)
                or not math.isfinite(value)
                or not 1 <= value <= 3600
            ):
                raise RuntimeErrorCode("INVALID_RECONCILIATION_INTERVAL")
        if retry_max < retry_base:
            raise RuntimeErrorCode("INVALID_RECONCILIATION_INTERVAL")
        self.store, self.policy = store, policy
        self.interval, self.retry_base, self.retry_max = interval, retry_base, retry_max

    async def claim(self):
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c)
            now = await self.store.db.now(c)
            query = (
                select(runs, work.c.id.label("authorization_id"))
                .join(
                    work, and_(work.c.run_id == runs.c.id, work.c.scope == runs.c.scope)
                )
                .outerjoin(
                    checks,
                    and_(checks.c.run_id == runs.c.id, checks.c.scope == runs.c.scope),
                )
                .where(
                    runs.c.scope == self.store.scope,
                    ~runs.c.state.in_(TERMINAL),
                    work.c.state == "active",
                    work.c.expires_at > now,
                    runs.c.input["principal"].as_string().is_not(None),
                    or_(
                        checks.c.run_id.is_(None),
                        and_(
                            checks.c.lease_until <= now, checks.c.next_check_at <= now
                        ),
                    ),
                )
                .order_by(
                    func.coalesce(checks.c.next_check_at, 0),
                    runs.c.created_at,
                    runs.c.id,
                )
                .limit(1)
            )
            row = (await c.execute(query)).mappings().first()
            if row is None:
                return None
            run = dict(row)
            old = (
                (await c.execute(select(checks).where(checks.c.run_id == run["id"])))
                .mappings()
                .first()
            )
            owner = str(uuid4())
            generation = old["generation"] + 1 if old else 1
            binding = run["input"]["principal"]
            fields = {"owner": owner, "generation": generation, "lease_until": now + 30}
            if old:
                await c.execute(
                    checks.update().where(checks.c.run_id == run["id"]).values(**fields)
                )
            else:
                await c.execute(
                    checks.insert().values(
                        run_id=run["id"],
                        scope=self.store.scope,
                        authorization_id=run["authorization_id"],
                        binding_digest=digest(binding),
                        next_check_at=now,
                        checked_at=None,
                        last_success_at=None,
                        outcome="pending",
                        reason=None,
                        failures=0,
                        **fields,
                    )
                )
            return {
                "run_id": run["id"],
                "authorization_id": run["authorization_id"],
                "binding": binding,
                "binding_digest": digest(binding),
                "owner": owner,
                "generation": generation,
            }

    def inspect(self, binding):
        try:
            provider = self.policy.identity_provider()
            if provider is None:
                raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")
            provider.current(binding)
            return "valid", "IDENTITY_SOURCE_CURRENT"
        except RuntimeErrorCode as exc:
            if exc.code in PERMANENT:
                return "revoked", exc.code
            return "unavailable", "AUTH_PROVIDER_UNAVAILABLE"
        except (OSError, ValueError, TypeError):
            return "unavailable", "AUTH_PROVIDER_UNAVAILABLE"

    async def finish(self, claim, outcome, reason):
        if (outcome, reason) not in {
            ("valid", "IDENTITY_SOURCE_CURRENT"),
            ("unavailable", "AUTH_PROVIDER_UNAVAILABLE"),
            ("revoked", "AUTH_PRINCIPAL_REVOKED"),
            ("revoked", "AUTH_PRINCIPAL_EXPIRED"),
        }:
            raise RuntimeErrorCode("INVALID_IDENTITY_OBSERVATION")
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c)
            now = await self.store.db.now(c)
            old = (
                (
                    await c.execute(
                        select(checks).where(
                            checks.c.run_id == claim["run_id"],
                            checks.c.scope == self.store.scope,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (
                old is None
                or (old["owner"], old["generation"])
                != (claim["owner"], claim["generation"])
                or old["lease_until"] <= now
            ):
                return {"run_id": claim["run_id"], "outcome": "stale"}
            run = await self.store._run(c, claim["run_id"])
            authority = (
                (
                    await c.execute(
                        select(work).where(
                            work.c.run_id == run["id"], work.c.scope == self.store.scope
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (
                authority is None
                or authority["id"] != claim["authorization_id"]
                or run["input"].get("work_authorization_ref")
                != claim["authorization_id"]
                or digest(run["input"].get("principal")) != claim["binding_digest"]
            ):
                raise RuntimeErrorCode("WORK_AUTHORIZATION_BINDING_MISMATCH")
            active = (
                authority["state"] == "active"
                and authority["expires_at"] > now
                and run["state"] not in TERMINAL
            )
            if not active:
                # Completion, cancellation and explicit revocation win over a delayed query.
                await c.execute(
                    checks.update()
                    .where(checks.c.run_id == run["id"])
                    .values(owner=None, lease_until=0)
                )
                return {"run_id": run["id"], "outcome": "superseded"}
            if outcome == "revoked":
                await persist_identity_revocation(c, self.store.scope, authority["id"])
            failures = old["failures"] + 1 if outcome == "unavailable" else 0
            wait = (
                min(self.retry_max, self.retry_base * 2 ** min(failures - 1, 12))
                if failures
                else self.interval
            )
            await c.execute(
                checks.update()
                .where(checks.c.run_id == run["id"])
                .values(
                    owner=None,
                    lease_until=0,
                    checked_at=now,
                    next_check_at=now + wait,
                    failures=failures,
                    last_success_at=now
                    if outcome == "valid"
                    else old["last_success_at"],
                    outcome=outcome,
                    reason=reason,
                )
            )
            if old["outcome"] != outcome or old["reason"] != reason:
                await audit(
                    c,
                    self.store.scope,
                    run["id"],
                    run["attempt_id"],
                    "identity.reconcile",
                    Decision(
                        "identity-observation",
                        "allow" if outcome == "valid" else "deny",
                        (reason,),
                    ),
                    now,
                )
            return {
                "run_id": run["id"],
                "outcome": outcome,
                "reason": reason,
                "checked_at": now,
                "next_check_at": now + wait,
            }

    async def batch(self, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RuntimeErrorCode("INVALID_LIMIT")
        rows = []
        for _ in range(limit):
            claim = await self.claim()
            if claim is None:
                break
            # Provider timeouts stay bounded; no database transaction spans network I/O.
            outcome, reason = await asyncio.to_thread(self.inspect, claim["binding"])
            rows.append(await self.finish(claim, outcome, reason))
        return {"items": rows, "checked": len(rows)}


async def observation(c, scope, run_id, now):
    if not await check(c):
        return None
    row = (
        (
            await c.execute(
                select(checks).where(checks.c.scope == scope, checks.c.run_id == run_id)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        k: row[k]
        for k in (
            "outcome",
            "reason",
            "checked_at",
            "last_success_at",
            "next_check_at",
            "failures",
        )
    } | {
        "checking": row["owner"] is not None and row["lease_until"] > now,
        "stale": row["next_check_at"] + 30 < now,
    }
