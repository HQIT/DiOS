"""Operator-only offline evidence report; no new execution or billing authority."""

from collections import Counter, defaultdict
import hashlib
import json
import math

from sqlalchemy import and_, func, or_, select

from . import consumption, diagnostics, usage
from .database import RuntimeErrorCode
from .object_schema import links
from .outcome_schema import outcomes
from .overview import TERMINAL, view as overview
from .revision_schema import revisions
from .schema import artifacts, attempts, runs
from .tool_schema import invocations


def checksum(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def coverage(present, eligible):
    return {
        "present": present,
        "eligible": eligible,
        "missing": eligible - present,
        "missing_fraction": (eligible - present) / eligible if eligible else None,
    }


def distribution(values):
    values = sorted(
        x for x in values if type(x) in (float, int) and math.isfinite(x) and x >= 0
    )
    return {
        "count": len(values),
        "sum_seconds": sum(values) if values else None,
        "p50_seconds": values[math.ceil(len(values) * 0.5) - 1] if values else None,
        "p95_seconds": values[math.ceil(len(values) * 0.95) - 1] if values else None,
        "max_seconds": max(values) if values else None,
    }


async def capture(db, scope, *, profile_id=None, limit=500):
    """Trusted local operator read. One DB transaction for selected-run evidence.

    The active-work overview is a separate, explicitly timestamped observation.
    No artifact bytes, arguments, principal data, secret refs or model text leave
    the read transaction. Raw IDs are retained for local evidence reconciliation.
    """
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise RuntimeErrorCode("INVALID_REPORT_LIMIT")
    condition = runs.c.scope == scope
    if profile_id:
        condition = and_(
            condition, runs.c.input["runtime_profile"].as_string() == profile_id
        )
    result = []
    async with db.read(scope) as c:
        now = await db.now(c)
        count = (
            await c.execute(select(func.count()).select_from(runs).where(condition))
        ).scalar_one()
        selected = (
            (
                await c.execute(
                    select(runs)
                    .where(condition)
                    .order_by(runs.c.created_at, runs.c.id)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        for run in selected:
            aid = select(attempts.c.id).where(attempts.c.run_id == run["id"])
            artifact_ids = select(artifacts.c.id).where(artifacts.c.run_id == run["id"])
            attempt_rows = (
                (
                    await c.execute(
                        select(
                            attempts.c.id,
                            attempts.c.worker_id,
                            attempts.c.worker_instance,
                            attempts.c.session_id,
                            attempts.c.state,
                            attempts.c.started_at,
                            attempts.c.finished_at,
                        ).where(attempts.c.run_id == run["id"])
                    )
                )
                .mappings()
                .all()
            )
            artifact_count = (
                await c.execute(
                    select(func.count())
                    .select_from(artifacts)
                    .where(artifacts.c.run_id == run["id"])
                )
            ).scalar_one()
            source_count = (
                await c.execute(
                    select(func.count())
                    .select_from(links)
                    .where(
                        links.c.scope == scope,
                        or_(
                            and_(
                                links.c.source_kind == "session",
                                links.c.source_id.in_(aid),
                            ),
                            and_(
                                links.c.source_kind == "artifact",
                                links.c.source_id.in_(artifact_ids),
                            ),
                        ),
                    )
                )
            ).scalar_one()
            tool_states = (
                await c.execute(
                    select(invocations.c.state, func.count())
                    .where(
                        invocations.c.scope == scope, invocations.c.run_id == run["id"]
                    )
                    .group_by(invocations.c.state)
                )
            ).all()
            outcome = (
                (
                    await c.execute(
                        select(
                            outcomes.c.state,
                            outcomes.c.finished_at,
                            outcomes.c.event_id,
                            outcomes.c.event_digest,
                        ).where(
                            outcomes.c.scope == scope, outcomes.c.run_id == run["id"]
                        )
                    )
                )
                .mappings()
                .first()
            )
            revision = run["input"].get("revision_ref")
            revision_exists = bool(
                revision
                and (
                    await c.execute(
                        select(revisions.c.id).where(
                            revisions.c.scope == scope, revisions.c.id == revision
                        )
                    )
                ).first()
            )
            diag = await diagnostics.view(c, scope, run, now)
            model_usage = await usage.view(c, scope, run["id"])
            cost = await consumption.run_view(c, scope, run)
            result.append(
                {
                    "id": run["id"],
                    "state": run["state"],
                    "version": run["version"],
                    "created_at": run["created_at"],
                    "profile_digest_recorded": bool(run["input"].get("profile_digest")),
                    "revision_recorded": revision_exists,
                    "authorization_recorded": bool(
                        run["input"].get("work_authorization_ref")
                    ),
                    "attempts": [
                        {**dict(a), "session_recorded": bool(a["session_id"])}
                        for a in attempt_rows
                    ],
                    "artifact_count": artifact_count,
                    "source_relation_count": source_count,
                    "tool_states": dict(tool_states),
                    "outcome": dict(outcome) if outcome else None,
                    "diagnostics": {
                        k: diag[k]
                        for k in (
                            "stages",
                            "governance",
                            "attempts_has_more",
                            "governance_has_more",
                        )
                    },
                    "usage": {
                        "at_limit": model_usage["at_limit"],
                        "items": [
                            {
                                k: u[k]
                                for k in (
                                    "attempt_id",
                                    "sequence",
                                    "kind",
                                    "quality",
                                    "values",
                                    "aggregation",
                                )
                            }
                            for u in model_usage["items"]
                        ],
                    },
                    "consumption": {
                        "at_limit": cost["at_limit"],
                        "items": [
                            {
                                **{
                                    k: x[k]
                                    for k in (
                                        "account_id",
                                        "attempt_id",
                                        "unit",
                                        "reserved",
                                        "actual",
                                        "state",
                                    )
                                },
                                "evidence_recorded": bool(x["evidence"]),
                            }
                            for x in cost["items"]
                        ],
                    },
                }
            )
    live = await overview(db, scope, condition, period="hour", buckets=24)
    return {
        "schema_version": 1,
        "captured_at": now,
        "selected_runs": len(result),
        "matching_runs": count,
        "runs_truncated": count > len(result),
        "filter": {"profile_id": profile_id},
        "runs": result,
        "active_observation": {"observed_at": live["observed_at"], **live["active"]},
        "limits": {
            "runs": limit,
            "attempts_per_diagnostics": 20,
            "governance_per_run": 100,
        },
        "basis": "trusted_operator_local_read; selected run records in one transaction",
    }


def summarize(data):
    rows = data["runs"]
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("duplicate run identity")
    stages, governance = defaultdict(list), defaultdict(list)
    closed = valid_closed = checkpoints = timed = sessions = reported = 0
    outcome_counts, hourly, daily = (
        Counter(),
        defaultdict(Counter),
        defaultdict(Counter),
    )
    usage_records, unknown_usage, settlements = [], [], []
    tool_states = Counter()
    truncated = bool(data["runs_truncated"])
    for run in rows:
        diag = run["diagnostics"]
        truncated |= (
            diag["attempts_has_more"]
            or diag["governance_has_more"]
            or run["usage"]["at_limit"]
            or run.get("consumption", {}).get("at_limit", False)
        )
        settlements.extend(
            {"run_id": run["id"], **x}
            for x in run.get("consumption", {}).get("items", [])
        )
        tool_states.update(run["tool_states"])
        for stage in diag["stages"]:
            if stage["ended_at"] is not None:
                closed += 1
                duration = stage["duration_seconds"]
                if (
                    type(duration) in (int, float)
                    and math.isfinite(duration)
                    and duration >= 0
                    and not stage["clock_anomaly"]
                ):
                    valid_closed += 1
                    stages[stage["phase"]].append(duration)
        for check in diag["governance"]:
            if check["action"] == "admit":
                continue
            checkpoints += 1
            timing = diagnostics.measured(check["timing"])
            if timing:
                timed += 1
                for key, value in timing.items():
                    if key != "clock":
                        governance[key].append(value)
        latest = {}
        for item in run["usage"]["items"]:
            key = (item["attempt_id"], item["kind"])
            if key not in latest or latest[key]["sequence"] < item["sequence"]:
                latest[key] = item
        for a in run["attempts"]:
            if a["session_recorded"]:
                sessions += 1
                available = [
                    v
                    for (aid, _), v in latest.items()
                    if aid == a["id"] and v["quality"] == "reported"
                ]
                if available:
                    reported += 1
                else:
                    unknown_usage.append(a["id"])
        usage_records.extend({"run_id": run["id"], **item} for item in latest.values())
        outcome = run["outcome"]
        if outcome:
            state, when = outcome["state"], outcome["finished_at"]
            outcome_counts[state] += 1
            hourly[math.floor(when / 3600) * 3600][state] += 1
            daily[math.floor(when / 86400) * 86400][state] += 1
    terminal = sum(r["state"] in TERMINAL for r in rows)
    active = data["active_observation"]
    return {
        "schema_version": 1,
        "capture_sha256": checksum(data),
        "captured_at": data["captured_at"],
        "selection": {
            k: data[k]
            for k in ("selected_runs", "matching_runs", "runs_truncated", "filter")
        },
        "bounded_records_complete": not truncated,
        "coverage_scope": "Selected runs; phase/checkpoint rates use returned bounded records. Truncation means whole-history rates are unknown.",
        "coverage": {
            "profile_digest_presence": coverage(
                sum(r["profile_digest_recorded"] for r in rows), len(rows)
            ),
            "revision_presence": coverage(
                sum(r["revision_recorded"] for r in rows), len(rows)
            ),
            "explicit_source_relation_presence": coverage(
                sum(r["source_relation_count"] > 0 for r in rows), len(rows)
            ),
            "terminal_outcome": coverage(
                sum(r["outcome"] is not None for r in rows if r["state"] in TERMINAL),
                terminal,
            ),
            "closed_phase_duration": coverage(valid_closed, closed),
            "audited_checkpoint_timing": coverage(timed, checkpoints),
            "session_usage_report": coverage(reported, sessions),
            "agent_internal_tool_events": None,
        },
        "stage_seconds": {k: distribution(v) for k, v in sorted(stages.items())},
        "governance_seconds": {
            k: distribution(v) for k, v in sorted(governance.items())
        },
        "throughput": {
            "basis": "first durable terminal outcome per selected Run; UTC",
            "counts": dict(outcome_counts),
            "hourly": [{"start": t, **dict(c)} for t, c in sorted(hourly.items())],
            "daily": [{"start": t, **dict(c)} for t, c in sorted(daily.items())],
        },
        "concurrency": {
            "observed_at": active["observed_at"],
            "actual": active["actual_concurrency"],
            "confirmed_sessions": active["confirmed_sessions"],
            "at_limit": active["at_limit"],
            "historical_peak": None,
        },
        "usage": {
            "evidence_level": "agent_reported",
            "aggregation": "latest per attempt/kind; unspecified scope snapshots are not summed",
            "records": usage_records,
            "unknown_attempts": unknown_usage,
            "settled_cost": None,
            "settlement_basis": "K04-2 Consumption ledger; model-reported cost is not a settlement",
        },
        "settlements": {
            "evidence_level": "operator_declared",
            "records": settlements,
            "aggregation": "per account and attempt; overlapping account scopes and currencies are not summed",
            "coverage": coverage(
                sum(
                    x["actual"] is not None and x["evidence_recorded"]
                    for x in settlements
                ),
                len(settlements),
            ),
        },
        "relations": {
            "artifact_records": sum(r["artifact_count"] for r in rows),
            "explicit_source_relations": sum(r["source_relation_count"] for r in rows),
            "tool_states": dict(tool_states),
            "agent_internal_tools": "unknown",
        },
        "boundaries": {
            "phase_clock": "database receipt time; includes transport and reporting delays",
            "checkpoint_clock": "monotonic; before_audit excludes audit write and commit",
            "opa_request": "transport plus evaluation and response validation",
            "storage_commit": "not observed in production audit",
            "protocol_vs_model_internal": "requires boundary probe; not inferred from waiting_model",
            "source_presence": "explicit relation availability, not proof of knowledge use or a required-source denominator",
            "quantile": "nearest-rank; nested checkpoint durations are not additive",
            "performance_scope": "observed selected workloads, not a capacity or model benchmark",
        },
    }


def markdown(report):
    lines = [
        "# DiOS 执行证据报告",
        "",
        f"匹配任务 {report['selection']['matching_runs']}，本报告包含 {report['selection']['selected_runs']}。",
        f"可复算输入 SHA-256：`{report['capture_sha256']}`。",
        "",
        "## 证据覆盖",
        "",
        "| 指标 | 有记录 / 分母 | 缺失率 |",
        "| --- | ---: | ---: |",
    ]
    for key, item in report["coverage"].items():
        if item is None:
            lines.append(f"| {key} | 未观测 | 未知 |")
        else:
            fraction = item["missing_fraction"]
            value = f"{fraction:.1%}" if fraction is not None else "无适用样本"
            lines.append(
                f"| {key} | {item['present']} / {item['eligible']} | {value} |"
            )
    for title, key in (
        ("阶段耗时（控制面收件时间）", "stage_seconds"),
        ("治理检查点耗时（单调时钟）", "governance_seconds"),
    ):
        lines += [
            "",
            f"## {title}",
            "",
            "| 阶段 | 样本 | P50 秒 | P95 秒 | 最大秒 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for name, value in report[key].items():
            lines.append(
                f"| {name} | {value['count']} | {value['p50_seconds']:.6f} | {value['p95_seconds']:.6f} | {value['max_seconds']:.6f} |"
            )
    lines += [
        "",
        "## 完成量与并发",
        "",
        f"首次终态记录：`{json.dumps(report['throughput']['counts'], ensure_ascii=False)}`。小时、日明细见 JSON。",
        f"当前实际并发：{report['concurrency']['actual'] if report['concurrency']['actual'] is not None else '未知'}；历史峰值未采集。",
        "",
        "用量保留每个尝试及类型的最新 Agent 报告；未知值保留为空。费用结算沿用 K04-2 账本。",
        "",
        "## 统计口径",
        "",
        "来源关联覆盖表示显式关系存在；Agent 内部工具事件缺失率未知。阶段时间包含上报延迟，模型内部等待与传输需结合边界探针判断。治理子阶段有包含关系，不能相加为总耗时。",
        "表内分母为所返回的记录。存在截断时，全历史阶段和检查点缺失率未知。",
        f"有界记录是否完整：{'是' if report['bounded_records_complete'] else '否，存在截断'}。",
    ]
    return "\n".join(lines) + "\n"
