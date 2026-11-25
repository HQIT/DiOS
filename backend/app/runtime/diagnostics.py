"""Bounded read models over durable phase events and measured governance evidence."""

import math

from sqlalchemy import and_, func, or_, select

from .schema import attempts, events
from .tool_schema import evidence, invocations
from .work_auth_schema import decisions


def measured(value):
    """Only server-generated numeric timing fields leave the audit record."""
    if not isinstance(value, dict) or value.get("clock") != "monotonic":
        return None
    allowed = (
        "lookup_seconds",
        "policy_refresh_seconds",
        "transaction_wait_seconds",
        "authorization_seconds",
        "before_audit_seconds",
        "opa_request_seconds",
    )
    result = {
        key: value[key]
        for key in allowed
        if key in value
        and type(value[key]) in (int, float)
        and math.isfinite(value[key])
        and value[key] >= 0
    }
    return {"clock": "monotonic", **result} if result else None


async def view(c, scope, run, now, *, include_tools=True):
    rows = (
        (
            await c.execute(
                select(attempts)
                .where(attempts.c.run_id == run["id"])
                .order_by(attempts.c.epoch.desc())
                .limit(21)
            )
        )
        .mappings()
        .all()
    )
    selected = rows[:20]
    phase = events.c.payload["detail"]["phase"].as_string()
    aid = events.c.payload["detail"]["attempt_id"].as_string()
    ranked = (
        select(
            aid.label("attempt_id"),
            phase.label("phase"),
            events.c.created_at,
            events.c.id,
            events.c.version,
            func.row_number()
            .over(partition_by=(aid, phase), order_by=events.c.version)
            .label("rank"),
        )
        .where(
            events.c.run_id == run["id"],
            events.c.type == "attempt.phase",
            aid.in_([a["id"] for a in selected]),
        )
        .subquery()
    )
    first = (
        (
            await c.execute(
                select(ranked).where(ranked.c.rank == 1).order_by(ranked.c.version)
            )
        )
        .mappings()
        .all()
    )
    grouped = {a["id"]: [] for a in selected}
    for item in first:
        grouped[item["attempt_id"]].append(item)
    stages = []
    for a in selected:
        starts = [{"phase": "claimed", "created_at": a["started_at"], "id": None}]
        starts += [x for x in grouped[a["id"]] if x["phase"] != "claimed"]
        for index, start in enumerate(starts):
            end = (
                starts[index + 1]["created_at"]
                if index + 1 < len(starts)
                else a["finished_at"]
            )
            anomaly = (end if end is not None else now) < start["created_at"]
            stages.append(
                {
                    "attempt_id": a["id"],
                    "epoch": a["epoch"],
                    "phase": start["phase"],
                    "event_id": start["id"],
                    "started_at": start["created_at"],
                    "ended_at": end,
                    "duration_seconds": end - start["created_at"]
                    if end is not None and not anomaly
                    else None,
                    "elapsed_seconds": now - start["created_at"]
                    if end is None and not anomaly
                    else None,
                    "clock_anomaly": anomaly,
                    "end_reason": "next_phase"
                    if index + 1 < len(starts)
                    else "attempt_finished"
                    if end is not None
                    else "open",
                }
            )
    work_rows = (
        (
            await c.execute(
                select(decisions)
                .where(decisions.c.scope == scope, decisions.c.run_id == run["id"])
                .order_by(decisions.c.created_at.desc(), decisions.c.id)
                .limit(101)
            )
        )
        .mappings()
        .all()
    )
    tool_rows = (
        (
            await c.execute(
                select(
                    evidence,
                    func.coalesce(
                        invocations.c.attempt_id,
                        evidence.c.decision["evidence"]["attempt_id"].as_string(),
                    ).label("attempt_id"),
                )
                .outerjoin(
                    invocations,
                    and_(
                        evidence.c.invocation_id == invocations.c.id,
                        invocations.c.scope == scope,
                    ),
                )
                .where(
                    evidence.c.scope == scope,
                    include_tools,
                    or_(
                        invocations.c.run_id == run["id"],
                        evidence.c.decision["evidence"]["run_id"].as_string()
                        == run["id"],
                    ),
                )
                .order_by(evidence.c.created_at.desc(), evidence.c.id)
                .limit(101)
            )
        )
        .mappings()
        .all()
    )
    governance = []
    for kind, records in (("work", work_rows), ("tool", tool_rows)):
        for row in records:
            value = row["decision"]
            timing = measured((value.get("evidence") or {}).get("timing"))
            governance.append(
                {
                    "id": row["id"],
                    "kind": kind,
                    "attempt_id": row["attempt_id"],
                    "action": row["action"] if kind == "work" else row["stage"],
                    "outcome": value["decision"],
                    "reason_codes": value["reason_codes"],
                    "created_at": row["created_at"],
                    "timing": timing,
                }
            )
    governance.sort(key=lambda x: (-x["created_at"], x["id"]))
    active = next((a for a in selected if a["id"] == run["attempt_id"]), None)
    observation = active["remote"].get("runtime", {}) if active else {}
    freshness = "no_worker"
    if run["state"] in {"succeeded", "failed", "cancelled"}:
        freshness = "final"
    elif active:
        freshness = (
            "fresh"
            if active["lease_until"] > now
            and active["state"] != "result_unknown"
            and observation.get("observed_at", 0) >= now - 15
            else "stale"
        )
    return {
        "time_basis": "control_plane_received",
        "observed_at": now,
        "last_event_at": run["updated_at"],
        "last_output_at": active["last_output_at"] if active else None,
        "freshness": freshness,
        "stages": stages,
        "attempts_has_more": len(rows) > 20,
        "governance": governance[:100],
        "governance_has_more": len(governance) > 100,
        "coverage": {
            "phases": "persisted_runtime_reports",
            "governance": "audited_checkpoints",
            "persistence_commit_seconds": None,
            "agent_internal_stages": "not_observed",
        },
    }
