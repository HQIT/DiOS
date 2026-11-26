"""Scope-atomic budget reservations; uncertain costs remain reserved until reconciled."""

import re

from sqlalchemy import and_, exists, func, not_, select

from .capacity import matches, validate_selector
from .consumption_schema import accounts, receipts, require, reservations, settlements
from .database import Database, RuntimeErrorCode
from .schema import attempts, runs
from .store import digest, identifier

MAXIMUM = 10**15


def quantity(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= MAXIMUM:
        raise RuntimeErrorCode("INVALID_CONSUMPTION_QUANTITY")
    return value


def where(table, scope, account_id):
    return and_(table.c.scope == scope, table.c.account_id == account_id)


async def balances(c, scope, account_id):
    resolved = exists(
        select(settlements.c.attempt_id).where(
            settlements.c.scope == reservations.c.scope,
            settlements.c.account_id == reservations.c.account_id,
            settlements.c.attempt_id == reservations.c.attempt_id,
        )
    )
    pending = (
        await c.execute(
            select(func.coalesce(func.sum(reservations.c.amount), 0)).where(
                where(reservations, scope, account_id), not_(resolved)
            )
        )
    ).scalar_one()
    posted = (
        await c.execute(
            select(func.coalesce(func.sum(settlements.c.actual), 0)).where(
                where(settlements, scope, account_id)
            )
        )
    ).scalar_one()
    unknown = (
        await c.execute(
            select(func.count())
            .select_from(
                reservations.join(attempts, attempts.c.id == reservations.c.attempt_id)
            )
            .where(
                where(reservations, scope, account_id),
                not_(resolved),
                attempts.c.state.not_in(
                    {
                        "claimed",
                        "preparing",
                        "creating_session",
                        "waiting_model",
                        "streaming",
                        "validating",
                    }
                ),
            )
        )
    ).scalar_one()
    return {
        "reserved": int(pending),
        "settled": int(posted),
        "unreconciled_attempts": unknown,
    }


def reason(account, balance):
    # No currently qualified Harness provides a provider-enforced monetary/token
    # ceiling. A declaration or operator boolean must never invent that guarantee.
    if account["enforcement"] == "required":
        return "PROVIDER_COST_CAP_UNSUPPORTED"
    if (
        balance["reserved"] + balance["settled"] + account["per_attempt"]
        > account["maximum"]
    ):
        return "CONSUMPTION_BUDGET_EXHAUSTED"
    return None


async def active(c, scope):
    await require(c)
    return (
        (
            await c.execute(
                select(accounts)
                .where(accounts.c.scope == scope, accounts.c.enabled.is_(True))
                .order_by(accounts.c.id)
            )
        )
        .mappings()
        .all()
    )


async def admit_query(c, scope, query):
    for row in await active(c, scope):
        if reason(row, await balances(c, scope, row["id"])):
            query = query.where(not_(matches(row["selector"], scope)))
    return query


async def bind(c, scope, run, attempt_id, now):
    """Called within the claim transaction, before exposing the new Attempt."""
    for row in await active(c, scope):
        applicable = (
            await c.execute(
                select(runs.c.id).where(
                    runs.c.id == run["id"],
                    runs.c.scope == scope,
                    matches(row["selector"], scope),
                )
            )
        ).first()
        if not applicable:
            continue
        blocked = reason(row, await balances(c, scope, row["id"]))
        if blocked:
            raise RuntimeErrorCode(blocked)
        await c.execute(
            reservations.insert().values(
                scope=scope,
                account_id=row["id"],
                run_id=run["id"],
                attempt_id=attempt_id,
                amount=row["per_attempt"],
                account_revision=row["revision"],
                created_at=now,
            )
        )


async def run_view(c, scope, run):
    await require(c)
    rows = (
        (
            await c.execute(
                select(
                    reservations,
                    accounts.c.unit,
                    settlements.c.actual,
                    settlements.c.evidence,
                )
                .select_from(
                    reservations.join(
                        accounts,
                        and_(
                            accounts.c.scope == reservations.c.scope,
                            accounts.c.id == reservations.c.account_id,
                        ),
                    ).outerjoin(
                        settlements,
                        and_(
                            settlements.c.scope == reservations.c.scope,
                            settlements.c.account_id == reservations.c.account_id,
                            settlements.c.attempt_id == reservations.c.attempt_id,
                        ),
                    )
                )
                .where(
                    reservations.c.scope == scope, reservations.c.run_id == run["id"]
                )
                .order_by(reservations.c.created_at.desc(), reservations.c.account_id)
                .limit(101)
            )
        )
        .mappings()
        .all()
    )
    blocked = []
    if run["state"] == "queued":
        for row in await active(c, scope):
            applicable = (
                await c.execute(
                    select(runs.c.id).where(
                        runs.c.id == run["id"],
                        runs.c.scope == scope,
                        matches(row["selector"], scope),
                    )
                )
            ).first()
            if applicable:
                why = reason(row, await balances(c, scope, row["id"]))
                if why:
                    blocked.append(why)
    return {
        "provider_hard_cap": "unsupported",
        "blocked_reasons": sorted(set(blocked)),
        "at_limit": len(rows) > 100,
        "items": [
            {
                "account_id": x["account_id"],
                "attempt_id": x["attempt_id"],
                "unit": x["unit"],
                "reserved": x["amount"],
                "actual": x["actual"],
                "state": "settled"
                if x["actual"] is not None
                else "reserved_unreconciled",
                "account_revision": x["account_revision"],
                "evidence": x["evidence"],
            }
            for x in rows[:100]
        ],
    }


class Consumption:
    """Trusted platform-operator accounting; model reports are not settlement authority."""

    def __init__(self, db, scope):
        identifier(scope)
        self.db, self.scope = db, scope

    async def _receipt(self, c, key, request):
        row = (
            (
                await c.execute(
                    select(receipts).where(
                        receipts.c.scope == self.scope, receipts.c.key == key
                    )
                )
            )
            .mappings()
            .first()
        )
        if row and row["digest"] != digest(request):
            raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
        return row["record"] if row else None

    async def _save(self, c, key, request, record, now):
        await c.execute(
            receipts.insert().values(
                scope=self.scope,
                key=key,
                digest=digest(request),
                record=record,
                created_at=now,
            )
        )
        return record

    async def put(
        self,
        account_id,
        selector,
        unit,
        maximum,
        per_attempt,
        expected_revision,
        *,
        key,
        actor,
        enforcement="required",
        enabled=True,
    ):
        for value in (account_id, key, actor):
            identifier(value)
        selector = validate_selector(selector)
        if not isinstance(unit, str) or not re.fullmatch(
            r"tokens|micro:[A-Z]{3}", unit
        ):
            raise RuntimeErrorCode("INVALID_CONSUMPTION_UNIT")
        quantity(maximum)
        quantity(per_attempt, positive=True)
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or type(enabled) is not bool
            or not isinstance(enforcement, str)
            or enforcement not in {"required", "reservation_only"}
        ):
            raise RuntimeErrorCode("INVALID_CONSUMPTION_ACCOUNT")
        request = [
            "put",
            account_id,
            selector,
            unit,
            maximum,
            per_attempt,
            expected_revision,
            actor,
            enforcement,
            enabled,
        ]
        async with self.db.transaction(self.scope) as c:
            await require(c)
            prior = await self._receipt(c, key, request)
            if prior:
                return prior
            old = (
                (
                    await c.execute(
                        select(accounts).where(
                            accounts.c.scope == self.scope, accounts.c.id == account_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if expected_revision != (old["revision"] if old else 0):
                raise RuntimeErrorCode("VERSION_CONFLICT")
            if old and (old["unit"] != unit or old["selector"] != selector):
                raise RuntimeErrorCode("CONSUMPTION_ACCOUNT_IDENTITY_IMMUTABLE")
            if (
                not old
                and (
                    await c.execute(
                        select(func.count())
                        .select_from(accounts)
                        .where(accounts.c.scope == self.scope)
                    )
                ).scalar_one()
                >= 64
            ):
                raise RuntimeErrorCode("CONSUMPTION_ACCOUNT_LIMIT")
            now = await self.db.now(c)
            row = dict(
                scope=self.scope,
                id=account_id,
                selector=selector,
                unit=unit,
                maximum=maximum,
                per_attempt=per_attempt,
                enforcement=enforcement,
                enabled=enabled,
                revision=expected_revision + 1,
                updated_at=now,
            )
            if old:
                await c.execute(
                    accounts.update()
                    .where(accounts.c.scope == self.scope, accounts.c.id == account_id)
                    .values(**row)
                )
            else:
                await c.execute(accounts.insert().values(**row))
            return await self._save(
                c,
                key,
                request,
                {"account": row, "before": dict(old) if old else None, "actor": actor},
                now,
            )

    async def settle(self, account_id, attempt_id, actual, evidence, *, key, actor):
        from .object_lifetime import ObjectLifetime
        from .objects import Objects, validate_ref

        for value in (account_id, attempt_id, key, actor):
            identifier(value)
        quantity(actual)
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"ref", "digest", "basis"}
            or not isinstance(evidence["basis"], str)
            or evidence["basis"]
            not in {"provider_statement", "operator_reconciliation"}
        ):
            raise RuntimeErrorCode("CONSUMPTION_EVIDENCE_REQUIRED")
        ref = validate_ref(evidence["ref"])
        if ref["kind"] not in {"file", "artifact"}:
            raise RuntimeErrorCode("CONSUMPTION_EVIDENCE_REQUIRED")
        request = ["settle", account_id, attempt_id, actual, evidence, actor]
        async with self.db.transaction(self.scope) as c:
            await require(c)
            prior = await self._receipt(c, key, request)
            if prior:
                return prior
            reserved = (
                (
                    await c.execute(
                        select(reservations).where(
                            where(reservations, self.scope, account_id),
                            reservations.c.attempt_id == attempt_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not reserved:
                raise RuntimeErrorCode("NOT_FOUND")
            old = (
                await c.execute(
                    select(settlements).where(
                        where(settlements, self.scope, account_id),
                        settlements.c.attempt_id == attempt_id,
                    )
                )
            ).first()
            if old:
                raise RuntimeErrorCode("CONSUMPTION_ALREADY_SETTLED")
            attempt = (
                (await c.execute(select(attempts).where(attempts.c.id == attempt_id)))
                .mappings()
                .one()
            )
            if attempt["state"] not in {"completed", "failed", "cancelled", "abandoned"}:
                raise RuntimeErrorCode("CONSUMPTION_EXECUTION_UNRESOLVED")
            obj = await Objects(None, self.scope)._get(c, ref)
            if obj["descriptor"]["digest"] != evidence["digest"]:
                raise RuntimeErrorCode("CONSUMPTION_EVIDENCE_DIGEST_MISMATCH")
            await ObjectLifetime(Database, self.scope).available(c, ref)
            now = await self.db.now(c)
            row = dict(
                scope=self.scope,
                account_id=account_id,
                attempt_id=attempt_id,
                actual=actual,
                evidence=evidence,
                actor=actor,
                created_at=now,
            )
            await c.execute(settlements.insert().values(**row))
            return await self._save(
                c,
                key,
                request,
                {
                    "settlement": row,
                    "reserved": reserved["amount"],
                    "over_reserved": actual > reserved["amount"],
                },
                now,
            )

    async def status(self):
        async with self.db.read(self.scope) as c:
            await require(c)
            rows = (
                (
                    await c.execute(
                        select(accounts)
                        .where(accounts.c.scope == self.scope)
                        .order_by(accounts.c.id)
                    )
                )
                .mappings()
                .all()
            )
            result = []
            for row in rows:
                totals = await balances(c, self.scope, row["id"])
                result.append(
                    dict(
                        row,
                        **totals,
                        remaining=row["maximum"]
                        - totals["settled"]
                        - totals["reserved"],
                        admission_error=reason(row, totals) if row["enabled"] else None,
                        provider_hard_cap="unsupported",
                    )
                )
            return {
                "scope": self.scope,
                "observed_at": await self.db.now(c),
                "items": result,
            }
