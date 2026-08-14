"""Evaluate causal-stage localization on real E2AG persisted traces."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(HERE))

from app.api.internal.e2ag_mcp import proxy_mcp_call  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.models.tables import A2ATask, Agent, E2AGApproval, E2AGToolGrant, EventLog, McpServer  # noqa: E402
from app.services.e2ag import verify_audit_chain  # noqa: E402
from app.services.e2ag_approval import decide_approval  # noqa: E402
from app.services.e2ag_tool_gateway import issue_task_mcp_config, validate_grant  # noqa: E402
from app.services.event_dispatcher import dispatch_event  # noqa: E402
from run_e2e_chain_experiment import CanaryAsyncClient, fake_send_message, request_for  # noqa: E402

FAULTS = (
    "contract_denied",
    "policy_denied",
    "approval_expired",
    "tool_grant_expired",
    "tool_call_denied",
)


def event(index: int, *, source: str = "github/team-alpha/repo", action: str = "git.read") -> dict:
    return {
        "specversion": "1.0",
        "id": f"audit-{index}",
        "source": source,
        "type": "git.push",
        "subject": f"audit/{index}",
        "data": {
            "requested_action": action,
            "requested_tool": "ledger.record_push",
            "environment": "production",
        },
    }


def localize(chain: list[dict]) -> str:
    """Return the earliest semantically terminal governance stage."""
    for entry in chain:
        stage = entry.get("stage")
        outcome = entry.get("outcome")
        if stage == "contract" and outcome == "deny":
            return "contract"
        if stage == "policy" and outcome == "deny":
            return "policy"
        if stage == "approval" and outcome in {"expired", "rejected"}:
            return "approval"
        if stage == "tool_grant" and outcome in {"grant_expired", "grant_revoked", "grant_withheld"}:
            return "tool_grant"
        if stage == "tool_call" and outcome == "deny":
            return "tool_call"
    return "none"


async def dispatch(db, agent: Agent, payload: dict) -> tuple[EventLog, A2ATask | None]:
    with patch("app.services.a2a_service.send_message", AsyncMock(side_effect=fake_send_message)):
        log, _ = await dispatch_event(payload, [agent.id], db)
    if log is None:
        raise RuntimeError("unexpected dedup")
    task = (
        await db.execute(select(A2ATask).where(A2ATask.context_id == log.id))
    ).scalar_one_or_none()
    return log, task


async def issue(db, agent: Agent, server: McpServer, task: A2ATask) -> tuple[E2AGToolGrant, str]:
    config, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
    if omissions:
        raise RuntimeError(str(omissions))
    connection = config[server.name]
    token = connection["headers"]["Authorization"].removeprefix("Bearer ")
    grant_id = connection["url"].rsplit("/", 1)[-1]
    grant = await db.get(E2AGToolGrant, grant_id)
    if grant is None:
        raise RuntimeError("grant not persisted")
    return grant, token


async def run_fault(db, agent: Agent, server: McpServer, fault: str, index: int) -> dict:
    if fault == "contract_denied":
        log, _ = await dispatch(db, agent, event(index, source="webhook/attacker"))
        expected = "contract"
        required = {"contract", "policy", "dispatch"}
    elif fault == "policy_denied":
        log, _ = await dispatch(db, agent, event(index, source="github/team-beta/repo"))
        expected = "policy"
        required = {"contract", "policy", "dispatch"}
    elif fault == "approval_expired":
        log, _ = await dispatch(db, agent, event(index, action="secret.read"))
        approval = (
            await db.execute(select(E2AGApproval).where(E2AGApproval.event_log_id == log.id))
        ).scalar_one()
        approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        try:
            await decide_approval(db, approval.id, decision="approved", actor="experiment")
        except ValueError as exc:
            if str(exc) != "APPROVAL_EXPIRED":
                raise
        await db.refresh(log)
        expected = "approval"
        required = {"contract", "policy", "dispatch", "approval"}
    elif fault == "tool_grant_expired":
        log, task = await dispatch(db, agent, event(index))
        if task is None:
            raise RuntimeError("task missing")
        grant, token = await issue(db, agent, server, task)
        grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        rejected, reason = await validate_grant(db, grant.id, token)
        if rejected is not None or reason != "TOOL_GRANT_EXPIRED":
            raise RuntimeError(f"unexpected expiry result: {rejected} {reason}")
        await db.refresh(log)
        expected = "tool_grant"
        required = {"contract", "policy", "a2a_task", "dispatch", "tool_grant"}
    elif fault == "tool_call_denied":
        log, task = await dispatch(db, agent, event(index))
        if task is None:
            raise RuntimeError("task missing")
        grant, token = await issue(db, agent, server, task)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "secret.read", "arguments": {"canary": "E2AG-CANARY"}},
        }
        with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", CanaryAsyncClient):
            response = await proxy_mcp_call(grant.id, request_for(payload, token), db)
        if response.status_code != 403:
            raise RuntimeError(f"unexpected tool response: {response.status_code}")
        await db.refresh(log)
        expected = "tool_call"
        required = {"contract", "policy", "a2a_task", "dispatch", "tool_grant", "tool_call"}
    else:
        raise ValueError(fault)

    chain = log.audit_chain or []
    observed = localize(chain)
    stages = [entry["stage"] for entry in chain]
    return {
        "fault": fault,
        "expected_stage": expected,
        "localized_stage": observed,
        "correct": observed == expected,
        "audit_valid": verify_audit_chain(chain),
        "trace_complete": required.issubset(set(stages)),
        "trace_id_consistent": all(entry.get("trace_id") == log.trace_id for entry in chain),
        "stages": stages,
        "outcomes": [entry["outcome"] for entry in chain],
    }


async def experiment(repeats: int) -> dict:
    old_mode = settings.e2ag_mode
    old_dedup = settings.event_dedup_enabled
    old_base = settings.e2ag_internal_base_url
    settings.e2ag_mode = "enforce"
    settings.event_dedup_enabled = False
    settings.e2ag_internal_base_url = "http://experiment.local"
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    rows = []
    try:
        async with sessions() as db:
            agent = Agent(
                id="audit-agent",
                name="Audit Experiment Agent",
                mode="task",
                capabilities={"governance": {
                    "allowed_event_sources": ["github/team-alpha/*"],
                    "allowed_event_types": ["git.push"],
                    "allowed_actions": ["git.read", "secret.read"],
                    "allowed_tools": ["ledger.record_*"],
                }},
            )
            server = McpServer(
                id="audit-mcp",
                name="audit-canary",
                transport="streamable_http",
                url="https://canary.invalid/mcp",
            )
            db.add_all([agent, server])
            await db.commit()
            index = 0
            for fault in FAULTS:
                for _ in range(repeats):
                    index += 1
                    rows.append(await run_fault(db, agent, server, fault, index))
        by_fault = {}
        for fault in FAULTS:
            selected = [row for row in rows if row["fault"] == fault]
            by_fault[fault] = {
                "traces": len(selected),
                "localized_correctly": sum(row["correct"] for row in selected),
                "audit_valid": sum(row["audit_valid"] for row in selected),
                "trace_complete": sum(row["trace_complete"] for row in selected),
                "trace_id_consistent": sum(row["trace_id_consistent"] for row in selected),
                "sample_stages": selected[0]["stages"],
                "sample_outcomes": selected[0]["outcomes"],
            }
        return {
            "protocol": "causal-stage-localization-v1",
            "repeats_per_fault": repeats,
            "traces": len(rows),
            "localization_accuracy": sum(row["correct"] for row in rows) / len(rows),
            "audit_valid_rate": sum(row["audit_valid"] for row in rows) / len(rows),
            "trace_complete_rate": sum(row["trace_complete"] for row in rows) / len(rows),
            "trace_id_consistency_rate": sum(row["trace_id_consistent"] for row in rows) / len(rows),
            "by_fault": by_fault,
            "limitations": [
                "Localization uses explicit E2AG stage/outcome semantics on deterministic injected faults.",
                "Hash-chain integrity does not detect tail truncation without an external anchor.",
                "This is causal-stage localization, not general program root-cause analysis.",
            ],
        }
    finally:
        settings.e2ag_mode = old_mode
        settings.event_dedup_enabled = old_dedup
        settings.e2ag_internal_base_url = old_base
        await engine.dispose()


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--output", type=Path, default=HERE / "results")
    args = parser.parse_args()
    result = await experiment(args.repeats)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "causal_audit_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(async_main())
