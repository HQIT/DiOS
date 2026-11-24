"""Bounded issuer reconciliation using the existing lease and backoff workflow."""

from uuid import uuid4
from sqlalchemy import select, and_, or_, func
from .database import RuntimeErrorCode
from .delegation_check_schema import checks, require, check
from .delegation_identity_schema import issuers
from .delegation_schema import links, events
from .delegation_identity import current_source
from .delegations import revoke_record
from .identity_reconciliation import IdentityReconciler, PERMANENT
from .logout_events import require_source
from .store import digest


def binding(row):
    return {
        key: row[key]
        for key in (
            "provider_path",
            "principal",
            "profile_id",
            "permissions",
            "expires_at",
        )
    }


class DelegationReconciler(IdentityReconciler):
    def __init__(self, store, **options):
        super().__init__(store, None, **options)

    async def claim(self):
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c)
            now = await self.store.db.now(c)
            row = (
                (
                    await c.execute(
                        select(
                            links,
                            issuers.c.provider_path,
                            issuers.c.principal,
                            issuers.c.profile_id,
                        )
                        .join(
                            issuers,
                            and_(
                                issuers.c.delegation_ref == links.c.id,
                                issuers.c.scope == links.c.scope,
                            ),
                        )
                        .outerjoin(
                            checks,
                            and_(
                                checks.c.delegation_ref == links.c.id,
                                checks.c.scope == links.c.scope,
                            ),
                        )
                        .where(
                            links.c.scope == self.store.scope,
                            links.c.state == "active",
                            links.c.expires_at > now,
                            or_(
                                checks.c.delegation_ref.is_(None),
                                and_(
                                    checks.c.lease_until <= now,
                                    checks.c.next_check_at <= now,
                                ),
                            ),
                        )
                        .order_by(
                            func.coalesce(checks.c.next_check_at, 0),
                            links.c.created_at,
                            links.c.id,
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            old = (
                (
                    await c.execute(
                        select(checks).where(checks.c.delegation_ref == row["id"])
                    )
                )
                .mappings()
                .first()
            )
            value = binding(row)
            hashed = digest(value)
            owner = str(uuid4())
            generation = old["generation"] + 1 if old else 1
            fields = dict(owner=owner, generation=generation, lease_until=now + 30)
            if old:
                if (
                    old["binding_digest"] != hashed
                    or old["authorization_version"] != row["version"]
                ):
                    raise RuntimeErrorCode("DELEGATION_BINDING_MISMATCH")
                await c.execute(
                    checks.update()
                    .where(checks.c.delegation_ref == row["id"])
                    .values(**fields)
                )
            else:
                await c.execute(
                    checks.insert().values(
                        delegation_ref=row["id"],
                        scope=self.store.scope,
                        authorization_version=row["version"],
                        binding_digest=hashed,
                        next_check_at=now,
                        checked_at=None,
                        last_success_at=None,
                        outcome="pending",
                        reason=None,
                        failures=0,
                        **fields,
                    )
                )
            return dict(
                delegation_ref=row["id"],
                binding=value,
                binding_digest=hashed,
                authorization_version=row["version"],
                owner=owner,
                generation=generation,
            )

    def inspect(self, value):
        try:
            current_source(value, value["permissions"], value["expires_at"])
            return "valid", "IDENTITY_SOURCE_CURRENT"
        except RuntimeErrorCode as exc:
            if exc.code in PERMANENT | {
                "AUTH_DELEGATION_DENIED",
                "AUTH_DELEGATION_DEADLINE",
            }:
                return "revoked", "AUTH_PRINCIPAL_REVOKED"
            return "unavailable", "AUTH_PROVIDER_UNAVAILABLE"
        except (OSError, ValueError, TypeError):
            return "unavailable", "AUTH_PROVIDER_UNAVAILABLE"

    async def finish(self, claim, outcome, reason):
        if (outcome, reason) not in {
            ("valid", "IDENTITY_SOURCE_CURRENT"),
            ("unavailable", "AUTH_PROVIDER_UNAVAILABLE"),
            ("revoked", "AUTH_PRINCIPAL_REVOKED"),
        }:
            raise RuntimeErrorCode("INVALID_IDENTITY_OBSERVATION")
        ref = claim["delegation_ref"]
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c)
            now = await self.store.db.now(c)
            old = (
                (
                    await c.execute(
                        select(checks).where(
                            checks.c.delegation_ref == ref,
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
                return dict(delegation_ref=ref, outcome="stale")
            row = (
                (
                    await c.execute(
                        select(
                            links,
                            issuers.c.provider_path,
                            issuers.c.principal,
                            issuers.c.profile_id,
                        )
                        .join(
                            issuers,
                            and_(
                                issuers.c.delegation_ref == links.c.id,
                                issuers.c.scope == links.c.scope,
                            ),
                        )
                        .where(links.c.id == ref, links.c.scope == self.store.scope)
                    )
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or digest(binding(row)) != claim["binding_digest"]
                or old["binding_digest"] != claim["binding_digest"]
            ):
                raise RuntimeErrorCode("DELEGATION_BINDING_MISMATCH")
            if (
                row["state"] != "active"
                or row["expires_at"] <= now
                or row["version"] != claim["authorization_version"]
            ):
                await c.execute(
                    checks.update()
                    .where(checks.c.delegation_ref == ref)
                    .values(owner=None, lease_until=0)
                )
                return dict(delegation_ref=ref, outcome="superseded")
            try:
                await require_source(c, self.store.scope, row["principal"])
            except RuntimeErrorCode as exc:
                if exc.code != "AUTH_PRINCIPAL_REVOKED":
                    raise
                outcome, reason = "revoked", "AUTH_PRINCIPAL_REVOKED"
            if outcome == "revoked":
                await revoke_record(
                    c, self.store.db, self.store.scope, row, reason=reason
                )
            failures = old["failures"] + 1 if outcome == "unavailable" else 0
            delay = (
                min(self.retry_max, self.retry_base * 2 ** min(failures - 1, 12))
                if failures
                else self.interval
            )
            await c.execute(
                checks.update()
                .where(checks.c.delegation_ref == ref)
                .values(
                    owner=None,
                    lease_until=0,
                    checked_at=now,
                    next_check_at=now + delay,
                    failures=failures,
                    outcome=outcome,
                    reason=reason,
                    last_success_at=now
                    if outcome == "valid"
                    else old["last_success_at"],
                )
            )
            if outcome != "revoked" and (old["outcome"], old["reason"]) != (
                outcome,
                reason,
            ):
                await c.execute(
                    events.insert().values(
                        id=str(uuid4()),
                        scope=self.store.scope,
                        delegation_ref=ref,
                        operation="identity_observed",
                        details=dict(outcome=outcome, reason=reason),
                        created_at=now,
                    )
                )
            return dict(
                delegation_ref=ref,
                outcome=outcome,
                reason=reason,
                checked_at=now,
                next_check_at=now + delay,
            )


async def observation(c, scope, ref, now):
    if not await check(c):
        return None
    row = (
        (
            await c.execute(
                select(checks).where(
                    checks.c.scope == scope, checks.c.delegation_ref == ref
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        key: row[key]
        for key in (
            "outcome",
            "reason",
            "checked_at",
            "last_success_at",
            "next_check_at",
            "failures",
        )
    } | dict(
        checking=row["owner"] is not None and row["lease_until"] > now,
        stale=row["next_check_at"] + 30 < now,
    )
