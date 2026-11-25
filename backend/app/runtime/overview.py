"""Durable logical-task throughput and evidence-based active-work observations."""

import math

from sqlalchemy import Integer, and_, cast, func, select

from .database import RuntimeErrorCode
from .fault_schema import plans
from .outcome_schema import outcomes, require
from .schema import attempts, runs, workers

TERMINAL = ("succeeded", "failed", "cancelled")


def observation(row, now):
    remote = row["remote"] if isinstance(row["remote"], dict) else {}
    runtime = remote.get("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
    observed = runtime.get("observed_at")
    if (
        type(observed) not in (int, float)
        or not math.isfinite(observed)
        or observed < 0
    ):
        observed = None
    heartbeat = row["heartbeat_at"]
    fresh = bool(
        row["attempt_id"]
        and row["lease_until"] > now
        and heartbeat is not None
        and now - 15 <= heartbeat <= now + 1
        and observed is not None
        and now - 15 <= observed <= now + 1
    )
    process = bool(
        fresh
        and runtime.get("alive") is True
        and type(runtime.get("pid")) is int
        and runtime["pid"] > 0
        and isinstance(runtime.get("process_instance"), str)
        and runtime["process_instance"]
    )
    session = bool(
        process
        and runtime.get("state") != "paused"
        and row["session_id"]
        and row["phase"] in {"waiting_model", "streaming", "validating"}
    )
    if row["state"] in {"queued", "retry_wait", "reconciliation_required"}:
        diagnosis = row["state"]
    elif row["phase"] == "result_unknown":
        diagnosis = "reconciliation_required"
    elif not fresh:
        diagnosis = "observation_stale"
    elif runtime.get("alive") is False and runtime.get("state") in {"exited", "dead"}:
        diagnosis = "process_exited"
    elif not process:
        diagnosis = "execution_unconfirmed"
    elif runtime.get("state") == "paused":
        diagnosis = "process_paused"
    elif (
        row["phase"] in {"waiting_model", "streaming"}
        and now - (row["last_output_at"] or row["started_at"]) >= 900
    ):
        diagnosis = "no_recent_output"
    else:
        diagnosis = row["phase"]
    return {
        k: row[k]
        for k in [
            "id",
            "profile_id",
            "state",
            "attempt_id",
            "phase",
            "last_output_at",
            "retry_at",
        ]
    } | {
        "diagnosis": diagnosis,
        "observed_at": observed,
        "heartbeat_at": heartbeat,
        "confirmed_session": session,
        "observation_fresh": fresh,
    }


async def view(db, scope, condition, *, period="hour", buckets=24):
    if (
        period not in {"hour", "day"}
        or type(buckets) is not int
        or not 1 <= buckets <= (168 if period == "hour" else 90)
    ):
        raise RuntimeErrorCode("INVALID_OVERVIEW_WINDOW")
    width = 3600 if period == "hour" else 86400
    async with db.read(scope) as c:
        await require(c)
        now = await db.now(c)
        current = math.floor(now / width)
        first = current - buckets + 1
        bucket = cast(func.floor(outcomes.c.finished_at / width), Integer)
        query = (
            select(
                bucket.label("bucket"), outcomes.c.state, func.count().label("count")
            )
            .join(runs, runs.c.id == outcomes.c.run_id)
            .where(
                outcomes.c.scope == scope,
                condition,
                outcomes.c.finished_at >= first * width,
                outcomes.c.finished_at <= now,
            )
        )
        history = (
            (await c.execute(query.group_by(bucket, outcomes.c.state))).mappings().all()
        )
        totals = (
            await c.execute(
                select(outcomes.c.state, func.count().label("count"))
                .join(runs, runs.c.id == outcomes.c.run_id)
                .where(outcomes.c.scope == scope, condition)
                .group_by(outcomes.c.state)
            )
        ).all()
        states = (
            await c.execute(
                select(runs.c.state, func.count().label("count"))
                .where(condition)
                .group_by(runs.c.state)
            )
        ).all()
        missing = (
            await c.execute(
                select(func.count())
                .select_from(runs)
                .outerjoin(
                    outcomes,
                    and_(outcomes.c.run_id == runs.c.id, outcomes.c.scope == scope),
                )
                .where(
                    condition, runs.c.state.in_(TERMINAL), outcomes.c.run_id.is_(None)
                )
            )
        ).scalar_one()
        active = (
            (
                await c.execute(
                    select(
                        runs.c.id,
                        runs.c.state,
                        runs.c.input["runtime_profile"].as_string().label("profile_id"),
                        attempts.c.id.label("attempt_id"),
                        attempts.c.state.label("phase"),
                        attempts.c.started_at,
                        attempts.c.lease_until,
                        attempts.c.session_id,
                        attempts.c.remote,
                        attempts.c.last_output_at,
                        workers.c.heartbeat_at,
                        plans.c.retry_at,
                    )
                    .outerjoin(
                        attempts,
                        and_(
                            attempts.c.id == runs.c.attempt_id,
                            attempts.c.run_id == runs.c.id,
                        ),
                    )
                    .outerjoin(
                        workers,
                        and_(
                            workers.c.id == attempts.c.worker_id,
                            workers.c.instance == attempts.c.worker_instance,
                            workers.c.scope == scope,
                        ),
                    )
                    .outerjoin(
                        plans, and_(plans.c.run_id == runs.c.id, plans.c.scope == scope)
                    )
                    .where(condition, runs.c.state.not_in(TERMINAL))
                    .order_by(runs.c.created_at, runs.c.id)
                    .limit(201)
                )
            )
            .mappings()
            .all()
        )
    series = {
        index: {
            "start": index * width,
            "end": min((index + 1) * width, now),
            **dict.fromkeys(TERMINAL, 0),
        }
        for index in range(first, current + 1)
    }
    for row in history:
        series[row["bucket"]][row["state"]] = row["count"]
    items = [observation(row, now) for row in active[:200]]
    unresolved = any(
        x["state"] not in {"queued", "retry_wait"}
        and (not x["observation_fresh"] or x["diagnosis"] == "execution_unconfirmed")
        for x in items
    )
    confirmed = sum(x["confirmed_session"] for x in items)
    return {
        "observed_at": now,
        "period": period,
        "timezone": "UTC",
        "window_start": first * width,
        "series": list(series.values()),
        "history_totals": dict.fromkeys(TERMINAL, 0) | dict(totals),
        "window_totals": {
            state: sum(item[state] for item in series.values()) for state in TERMINAL
        },
        "states": dict(states),
        "coverage": {
            "missing_terminal_outcomes": missing,
            "complete": missing == 0,
            "basis": "first_terminal_transition_per_run",
            "imported_history": "excluded",
        },
        "active": {
            "items": items,
            "at_limit": len(active) > 200,
            "confirmed_sessions": confirmed,
            "actual_concurrency": None
            if unresolved or len(active) > 200
            else confirmed,
            "silence_threshold_seconds": 900,
            "freshness_seconds": 15,
        },
    }
