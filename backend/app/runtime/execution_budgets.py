"""Database-clock rolling admission windows and execution deadlines across retries."""

from sqlalchemy import and_, exists, func, not_, select

from .database import RuntimeErrorCode
from .execution_budget_schema import bindings, policies, require
from .schema import attempts, runs

FIELDS = {"max_runtime_seconds", "max_dispatches", "dispatch_window_seconds"}


def validate(value):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise RuntimeErrorCode("INVALID_EXECUTION_BUDGET")
    result = {key: value.get(key) for key in FIELDS}
    for key, maximum in (
        ("max_runtime_seconds", 604800),
        ("max_dispatches", 100000),
        ("dispatch_window_seconds", 86400),
    ):
        val = result[key]
        if val is not None and (type(val) is not int or not 1 <= val <= maximum):
            raise RuntimeErrorCode("INVALID_EXECUTION_BUDGET")
    if (result["max_dispatches"] is None) != (
        result["dispatch_window_seconds"] is None
    ):
        raise RuntimeErrorCode("INVALID_EXECUTION_BUDGET")
    return result


async def policy(c, scope, limit_id):
    await require(c)
    row = (
        (
            await c.execute(
                select(policies).where(
                    policies.c.scope == scope, policies.c.limit_id == limit_id
                )
            )
        )
        .mappings()
        .first()
    )
    return {key: row[key] if row else None for key in FIELDS}


async def put_policy(c, scope, limit_id, value):
    value = validate(value)
    old = (
        await c.execute(
            select(policies.c.limit_id).where(
                policies.c.scope == scope, policies.c.limit_id == limit_id
            )
        )
    ).first()
    if old:
        await c.execute(
            policies.update()
            .where(policies.c.scope == scope, policies.c.limit_id == limit_id)
            .values(**value)
        )
    else:
        await c.execute(
            policies.insert().values(scope=scope, limit_id=limit_id, **value)
        )
    return value


async def rate_status(c, scope, selector, budget, now):
    if budget["max_dispatches"] is None:
        return None
    from .capacity import matches

    window, maximum = budget["dispatch_window_seconds"], budget["max_dispatches"]
    query = (
        select(attempts.c.started_at)
        .select_from(attempts.join(runs, runs.c.id == attempts.c.run_id))
        .where(
            runs.c.scope == scope,
            matches(selector, scope),
            attempts.c.started_at > now - window,
        )
    )
    count = (
        await c.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    available_at = None
    if count >= maximum:
        # A lowered cap can require several older admissions to leave the window.
        boundary = (
            await c.execute(
                query.order_by(attempts.c.started_at).offset(count - maximum).limit(1)
            )
        ).scalar_one()
        available_at = boundary + window
    return {
        "used": count,
        "maximum": maximum,
        "window_seconds": window,
        "available_at": available_at,
    }


async def live_limits(c, scope):
    from .capacity_schema import limits

    await require(c)
    return (
        (
            await c.execute(
                select(
                    limits,
                    policies.c.max_runtime_seconds,
                    policies.c.max_dispatches,
                    policies.c.dispatch_window_seconds,
                )
                .outerjoin(
                    policies,
                    and_(
                        policies.c.scope == limits.c.scope,
                        policies.c.limit_id == limits.c.id,
                    ),
                )
                .where(limits.c.scope == scope, limits.c.enabled.is_(True))
                .order_by(limits.c.id)
            )
        )
        .mappings()
        .all()
    )


async def matching(c, scope, run):
    from .capacity import matches

    result = []
    for row in await live_limits(c, scope):
        if (
            await c.execute(
                select(runs.c.id).where(
                    runs.c.id == run["id"], matches(row["selector"], scope)
                )
            )
        ).first():
            result.append(row)
    return result


async def admit_query(c, scope, query, now):
    from .capacity import matches

    for row in await live_limits(c, scope):
        rate = await rate_status(c, scope, row["selector"], row, now)
        if rate and rate["available_at"] is not None:
            query = query.where(not_(matches(row["selector"], scope)))
    return query.where(
        not_(
            exists(
                select(bindings.c.run_id).where(
                    bindings.c.run_id == runs.c.id,
                    bindings.c.scope == scope,
                    bindings.c.deadline_at <= now,
                )
            )
        )
    )


async def bind(c, scope, run, now):
    await require(c)
    if (
        await c.execute(
            select(bindings.c.run_id).where(
                bindings.c.run_id == run["id"], bindings.c.scope == scope
            )
        )
    ).first():
        await require_execution(c, run, now)
        return
    first = (
        await c.execute(
            select(func.min(attempts.c.started_at)).where(
                attempts.c.run_id == run["id"]
            )
        )
    ).scalar_one()
    first = now if first is None else first
    limits = [
        {
            "id": row["id"],
            "revision": row["revision"],
            "max_runtime_seconds": row["max_runtime_seconds"],
        }
        for row in await matching(c, scope, run)
        if row["max_runtime_seconds"] is not None
    ]
    deadline = min((first + row["max_runtime_seconds"] for row in limits), default=None)
    await c.execute(
        bindings.insert().values(
            run_id=run["id"],
            scope=scope,
            started_at=first,
            deadline_at=deadline,
            limits=limits,
        )
    )
    # Caller settles an already-expired legacy retry without creating an Attempt.
    return deadline


async def view(c, scope, run_id, now):
    await require(c)
    row = (
        (
            await c.execute(
                select(bindings).where(
                    bindings.c.scope == scope, bindings.c.run_id == run_id
                )
            )
        )
        .mappings()
        .first()
    )
    if not row or row["deadline_at"] is None:
        return None
    return {
        "started_at": row["started_at"],
        "deadline_at": row["deadline_at"],
        "remaining_seconds": max(0, row["deadline_at"] - now),
        "exhausted": row["deadline_at"] <= now,
    }


async def require_execution(c, run, now):
    value = await view(c, run["scope"], run["id"], now)
    if value and value["exhausted"]:
        raise RuntimeErrorCode("RUN_BUDGET_EXHAUSTED")


async def expire_waiting(c, store, now):
    await require(c)
    rows = (
        (
            await c.execute(
                select(runs)
                .join(bindings, bindings.c.run_id == runs.c.id)
                .where(
                    runs.c.scope == store.scope,
                    bindings.c.scope == store.scope,
                    runs.c.state.in_(["queued", "retry_wait"]),
                    bindings.c.deadline_at <= now,
                )
                .order_by(runs.c.created_at, runs.c.id)
                .limit(64)
            )
        )
        .mappings()
        .all()
    )
    for run in rows:
        await store._event(
            c, dict(run), "run.execution_budget_exhausted", {}, now, state="failed"
        )
