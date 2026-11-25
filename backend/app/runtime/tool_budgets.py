"""Tool dispatch admission uses the durable invocation ledger in its transaction."""

from sqlalchemy import func, select

from .database import RuntimeErrorCode
from .tool_budget_schema import bindings, policies, require
from .tool_schema import invocations


async def policy(c, scope, limit_id):
    await require(c)
    return (
        await c.execute(
            select(policies.c.max_tool_calls).where(
                policies.c.scope == scope, policies.c.limit_id == limit_id
            )
        )
    ).scalar_one_or_none()


async def put_policy(c, scope, limit_id, maximum):
    await require(c)
    where = (policies.c.scope == scope, policies.c.limit_id == limit_id)
    if (await c.execute(select(policies.c.limit_id).where(*where))).first():
        await c.execute(policies.update().where(*where).values(max_tool_calls=maximum))
    else:
        await c.execute(
            policies.insert().values(
                scope=scope, limit_id=limit_id, max_tool_calls=maximum
            )
        )


async def bind(c, scope, run):
    await require(c)
    old = (
        (
            await c.execute(
                select(bindings).where(
                    bindings.c.scope == scope, bindings.c.run_id == run["id"]
                )
            )
        )
        .mappings()
        .first()
    )
    if old:
        return dict(old)
    from .execution_budgets import matching

    limits = []
    for row in await matching(c, scope, run):
        maximum = await policy(c, scope, row["id"])
        if maximum is not None:
            limits.append(
                {
                    "id": row["id"],
                    "revision": row["revision"],
                    "max_tool_calls": maximum,
                }
            )
    record = {
        "scope": scope,
        "run_id": run["id"],
        "limits": limits,
        "max_tool_calls": min((x["max_tool_calls"] for x in limits), default=None),
    }
    await c.execute(bindings.insert().values(**record))
    return record


async def usage(c, scope, run_id):
    # dispatch_deadline is set once at commit, retained through receipt recovery.
    # A terminal unstarted call has no dispatch_deadline and consumes no budget.
    rows = (
        await c.execute(
            select(invocations.c.state, func.count())
            .where(
                invocations.c.scope == scope,
                invocations.c.run_id == run_id,
                invocations.c.dispatch_deadline.is_not(None),
            )
            .group_by(invocations.c.state)
        )
    ).all()
    counts = dict(rows)
    return {
        "committed": sum(counts.values()),
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
        "unresolved": counts.get("dispatched", 0) + counts.get("result_unknown", 0),
    }


async def view(c, scope, run_id):
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
    if not row:
        return None
    value = await usage(c, scope, run_id)
    maximum = row["max_tool_calls"]
    return dict(
        value,
        maximum=maximum,
        remaining=None if maximum is None else max(0, maximum - value["committed"]),
        exhausted=maximum is not None and value["committed"] >= maximum,
    )


async def require_dispatch(c, scope, run):
    bound = await bind(c, scope, run)
    value = await usage(c, scope, run["id"])
    maximum = bound["max_tool_calls"]
    if maximum is not None and value["committed"] >= maximum:
        raise RuntimeErrorCode("TOOL_BUDGET_EXHAUSTED")
    return {
        "maximum": maximum, "committed_before": value["committed"], "unit": "dispatch_admission"
    }
