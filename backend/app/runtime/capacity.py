"""Scope-serialized admission; persisted Attempts are the capacity ledger."""

from sqlalchemy import exists, func, not_, select, true

from .capacity_schema import changes, limits, require
from .database import RuntimeErrorCode
from .schema import attempts, runs
from .store import ACTIVE_ATTEMPTS, identifier
from .work_auth_schema import work

OCCUPIED = ACTIVE_ATTEMPTS | {"result_unknown"}


def validate_selector(value):
    if not isinstance(value, dict):
        raise RuntimeErrorCode("INVALID_CAPACITY_SELECTOR")
    if value == {"kind": "scope"}:
        return dict(value)
    if set(value) == {"kind", "subject"} and value["kind"] == "subject":
        identifier(value["subject"])
        return dict(value)
    if set(value) == {"kind", "profiles"} and value["kind"] == "profiles":
        profiles = value["profiles"]
        if isinstance(profiles, list) and 1 <= len(profiles) <= 32:
            for profile in profiles:
                identifier(profile)
            if len(set(profiles)) == len(profiles):
                return {"kind": "profiles", "profiles": sorted(profiles)}
    raise RuntimeErrorCode("INVALID_CAPACITY_SELECTOR")


def matches(selector, scope):
    if selector["kind"] == "scope":
        return true()
    if selector["kind"] == "subject":
        # Subject comes from accepted Work, never a workload-supplied payload.
        return exists(
            select(work.c.id).where(
                work.c.run_id == runs.c.id,
                work.c.scope == scope,
                work.c.subject == selector["subject"],
            )
        )
    return func.coalesce(runs.c.input["runtime_profile"].as_string(), "").in_(
        selector["profiles"]
    )


async def occupancy(c, scope, selector):
    rows = (
        (
            await c.execute(
                select(attempts.c.state, func.count().label("count"))
                .select_from(attempts.join(runs, attempts.c.run_id == runs.c.id))
                .where(
                    runs.c.scope == scope,
                    attempts.c.state.in_(OCCUPIED),
                    matches(selector, scope),
                )
                .group_by(attempts.c.state)
            )
        )
        .mappings()
        .all()
    )
    by_state = {row["state"]: row["count"] for row in rows}
    return sum(by_state.values()), by_state.get("result_unknown", 0)


async def admit_query(c, scope, query):
    """Caller holds the same transaction through Attempt insertion."""
    await require(c)
    rows = (
        (
            await c.execute(
                select(limits).where(
                    limits.c.scope == scope, limits.c.enabled.is_(True)
                )
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        occupied, _ = await occupancy(c, scope, row["selector"])
        if occupied >= row["max_concurrent"]:
            # Filter in SQL: an arbitrarily long blocked queue prefix cannot starve
            # a different subject/profile that still has capacity.
            query = query.where(not_(matches(row["selector"], scope)))
    return query


async def queue_status(c, scope, run, *, operator=False):
    """Explain a visible queued Run without exposing other subjects' usage."""
    if run["state"] != "queued":
        return None
    await require(c)
    subject = (
        await c.execute(
            select(work.c.subject).where(
                work.c.scope == scope, work.c.run_id == run["id"]
            )
        )
    ).scalar_one_or_none()
    rows = (
        (
            await c.execute(
                select(limits)
                .where(limits.c.scope == scope, limits.c.enabled.is_(True))
                .order_by(limits.c.id)
            )
        )
        .mappings()
        .all()
    )
    blocked = []
    from .database import Database
    from .execution_budgets import policy, rate_status

    now = await Database.now(c)
    for row in rows:
        selector = row["selector"]
        if selector["kind"] == "subject" and selector["subject"] != subject:
            continue
        if (
            selector["kind"] == "profiles"
            and run["input"].get("runtime_profile") not in selector["profiles"]
        ):
            continue
        occupied, unknown = await occupancy(c, scope, selector)
        budget = await policy(c, scope, row["id"])
        rate = await rate_status(c, scope, selector, budget, now)
        if occupied >= row["max_concurrent"]:
            blocked.append(
                {
                    "id": row["id"],
                    "max_concurrent": row["max_concurrent"],
                    "reserved_attempts": occupied,
                    "unresolved_attempts": unknown,
                }
            )
        if rate and rate["available_at"] is not None:
            blocked.append(
                {
                    "id": row["id"],
                    "reason": "DISPATCH_RATE_LIMITED",
                    "available_at": rate["available_at"],
                    "max_concurrent": row["max_concurrent"],
                    "reserved_attempts": occupied,
                    "unresolved_attempts": unknown,
                }
            )
    rate_only = bool(blocked) and all(
        x.get("reason") == "DISPATCH_RATE_LIMITED" for x in blocked
    )
    return {
        "blocked": bool(blocked),
        "reason": (
            "DISPATCH_RATE_LIMITED" if rate_only else "SHARED_CAPACITY_EXHAUSTED"
        )
        if blocked
        else None,
        **(
            {"available_at": max(x["available_at"] for x in blocked)}
            if rate_only
            else {}
        ),
        "limits": blocked if operator else [],
    }


class Capacity:
    """Trusted local administration, using the existing DB authority boundary."""

    def __init__(self, store):
        self.store = store

    async def put(
        self,
        limit_id,
        selector,
        max_concurrent,
        expected_revision,
        *,
        enabled=True,
        source="trusted_local",
        budget=None,
    ):
        identifier(limit_id)
        if source not in {"trusted_local", "operator_api"}:
            raise RuntimeErrorCode("INVALID_CAPACITY_SOURCE")
        selector = validate_selector(selector)
        from .execution_budgets import policy, put_policy, validate

        if budget is not None:
            budget = validate(budget)
        if (
            type(max_concurrent) is not int
            or not 0 <= max_concurrent <= 100000
            or type(expected_revision) is not int
            or expected_revision < 0
            or type(enabled) is not bool
        ):
            raise RuntimeErrorCode("INVALID_CAPACITY_LIMIT")
        scope = self.store.scope
        async with self.store.db.transaction(scope) as c:
            await require(c)
            old = (
                (
                    await c.execute(
                        select(limits).where(
                            limits.c.scope == scope, limits.c.id == limit_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if expected_revision != (old["revision"] if old else 0):
                raise RuntimeErrorCode("VERSION_CONFLICT")
            if old is None:
                count = (
                    await c.execute(
                        select(func.count())
                        .select_from(limits)
                        .where(limits.c.scope == scope)
                    )
                ).scalar_one()
                if count >= 64:
                    raise RuntimeErrorCode("CAPACITY_LIMIT_COUNT_EXCEEDED")
            now = await self.store.db.now(c)
            old_budget = await policy(c, scope, limit_id)
            record = {
                "scope": scope,
                "id": limit_id,
                "selector": selector,
                "max_concurrent": max_concurrent,
                "enabled": enabled,
                "revision": expected_revision + 1,
                "updated_at": now,
            }
            if old:
                await c.execute(
                    limits.update()
                    .where(limits.c.scope == scope, limits.c.id == limit_id)
                    .values(**record)
                )
            else:
                await c.execute(limits.insert().values(**record))
            record["budget"] = await put_policy(
                c, scope, limit_id, old_budget if budget is None else budget
            )
            await c.execute(
                changes.insert().values(
                    scope=scope,
                    limit_id=limit_id,
                    revision=record["revision"],
                    record={
                        "source": source,
                        "before": dict(old, budget=old_budget) if old else None,
                        "after": record,
                    },
                    created_at=now,
                )
            )
            return record

    async def status(self):
        scope = self.store.scope
        async with self.store.db.read(scope) as c:
            await require(c)
            rows = (
                (
                    await c.execute(
                        select(limits)
                        .where(limits.c.scope == scope)
                        .order_by(limits.c.id)
                    )
                )
                .mappings()
                .all()
            )
            result = []
            for row in rows:
                occupied, unknown = await occupancy(c, scope, row["selector"])
                from .execution_budgets import policy, rate_status

                budget = await policy(c, scope, row["id"])
                rate = await rate_status(
                    c, scope, row["selector"], budget, await self.store.db.now(c)
                )
                waiting = (
                    await c.execute(
                        select(func.count())
                        .select_from(runs)
                        .where(
                            runs.c.scope == scope,
                            runs.c.state == "queued",
                            runs.c.revoked.is_(False),
                            runs.c.cancel_requested.is_(False),
                            matches(row["selector"], scope),
                        )
                    )
                ).scalar_one()
                result.append(
                    dict(
                        row,
                        budget=budget,
                        dispatch_rate=rate,
                        reserved_attempts=occupied,
                        unresolved_attempts=unknown,
                        queued_runs=waiting,
                        available=max(0, row["max_concurrent"] - occupied)
                        if row["enabled"]
                        else None,
                    )
                )
            return {
                "scope": scope,
                "observed_at": await self.store.db.now(c),
                "items": result,
            }
