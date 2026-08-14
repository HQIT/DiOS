"""Run 480 deterministic Event -> Agent -> MCP tool-chain executions.

The experiment uses the real dispatcher, SQLite models, A2A Task persistence,
task-scoped ToolGrant issuance, MCP proxy authorization, and audit chain.  The
Agent decision and remote MCP server are deterministic test doubles so the
mechanism is measured independently from LLM and network variability.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from app.api.internal.e2ag_mcp import proxy_mcp_call  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.models.tables import A2ATask, Agent, E2AGApproval, E2AGToolGrant, EventLog, McpServer  # noqa: E402
from app.services import a2a_service  # noqa: E402
from app.services.e2ag import verify_audit_chain  # noqa: E402
from app.services.e2ag_tool_gateway import issue_task_mcp_config  # noqa: E402
from app.services.event_dispatcher import dispatch_event  # noqa: E402

CONFIGS = (
    ("G0R0", False, False),
    ("G1R0", True, False),
    ("G0R1", False, True),
    ("G1R1", True, True),
)

SCENARIOS = {
    "normal_authorized": {
        "event": {
            "source": "github/team-alpha/repo",
            "type": "git.push",
            "data": {
                "requested_action": "git.read",
                "requested_tool": "ledger.record_push",
                "environment": "production",
            },
        },
        "actual_tool": "ledger.record_push",
        "effect": "allowed",
    },
    "spoofed_source": {
        "event": {
            "source": "webhook/attacker",
            "type": "git.push",
            "data": {
                "requested_action": "git.read",
                "requested_tool": "ledger.record_push",
                "environment": "production",
            },
        },
        "actual_tool": "ledger.record_push",
        "effect": "forbidden",
    },
    "post_admission_tool_escalation": {
        "event": {
            "source": "github/team-alpha/repo",
            "type": "git.push",
            "data": {
                "requested_action": "git.read",
                "requested_tool": "ledger.record_push",
                "environment": "production",
            },
        },
        "actual_tool": "secret.read",
        "effect": "forbidden",
    },
    "production_sensitive_action": {
        "event": {
            "source": "github/team-alpha/repo",
            "type": "git.push",
            "data": {
                "requested_action": "secret.read",
                "requested_tool": "ledger.record_push",
                "environment": "production",
            },
        },
        "actual_tool": "secret.read",
        "effect": "forbidden",
    },
}


def request_for(payload: dict, token: str) -> Request:
    body = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/internal/e2ag/mcp/experiment",
            "raw_path": b"/api/internal/e2ag/mcp/experiment",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"content-type", b"application/json"),
            ],
            "client": ("experiment", 1),
            "server": ("experiment", 80),
        },
        receive,
    )


class CanaryAsyncClient:
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        tool = str(((json or {}).get("params") or {}).get("name") or "")
        self.__class__.calls.append({"url": url, "tool": tool, "headers": headers or {}})
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": (json or {}).get("id"), "result": {"tool": tool, "effect": True}},
            headers={"content-type": "application/json"},
        )


async def fake_send_message(*, db, agent_id, message, context_id, trace_id):
    return await a2a_service.create_task(
        db,
        agent_id=agent_id,
        message=message,
        context_id=context_id,
        trace_id=trace_id,
    )


def make_event(scenario: str, index: int) -> dict:
    template = SCENARIOS[scenario]["event"]
    return {
        "specversion": "1.0",
        "id": f"e2e-{scenario}-{index}",
        "source": template["source"],
        "type": template["type"],
        "subject": f"experiment/{scenario}/{index}",
        "data": dict(template["data"]),
    }


async def direct_upstream(server: McpServer, payload: dict) -> int:
    async with CanaryAsyncClient(trust_env=False, timeout=30) as client:
        response = await client.post(server.url, json=payload, headers=server.headers or {})
    return response.status_code


async def invoke_runtime_gate(db, agent: Agent, server: McpServer, task: A2ATask, tool: str) -> tuple[int, bool]:
    settings.e2ag_mode = "enforce"
    config, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
    if omissions:
        raise RuntimeError(f"unexpected grant omission: {omissions}")
    connection = config[server.name]
    token = connection["headers"]["Authorization"].removeprefix("Bearer ")
    grant_id = connection["url"].rsplit("/", 1)[-1]
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"canary": "E2AG-CANARY"}},
    }
    before = len(CanaryAsyncClient.calls)
    with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", CanaryAsyncClient):
        response = await proxy_mcp_call(grant_id, request_for(payload, token), db)
    return response.status_code, len(CanaryAsyncClient.calls) > before


async def run_once(db, agent: Agent, server: McpServer, scenario: str, index: int, pre_gate: bool, runtime_gate: bool) -> dict:
    settings.e2ag_mode = "enforce" if pre_gate else "off"
    event = make_event(scenario, index)
    with patch("app.services.a2a_service.send_message", AsyncMock(side_effect=fake_send_message)):
        event_log, error = await dispatch_event(event, [agent.id], db)
    if event_log is None:
        raise RuntimeError(f"unexpected dedup in {scenario}: {error}")

    tasks = (
        await db.execute(select(A2ATask).where(A2ATask.context_id == event_log.id))
    ).scalars().all()
    task = tasks[0] if tasks else None
    before = len(CanaryAsyncClient.calls)
    response_status = None
    upstream_reached = False
    if task is not None:
        tool = SCENARIOS[scenario]["actual_tool"]
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"canary": "E2AG-CANARY"}},
        }
        if runtime_gate:
            response_status, upstream_reached = await invoke_runtime_gate(db, agent, server, task, tool)
        else:
            response_status = await direct_upstream(server, payload)
            upstream_reached = len(CanaryAsyncClient.calls) > before

    await db.refresh(event_log)
    audit = event_log.audit_chain or []
    stages = [entry["stage"] for entry in audit]
    expected_stages = {"contract", "policy", "dispatch"}
    if event_log.status == "approval_required":
        expected_stages.add("approval")
    if task is not None:
        expected_stages.add("a2a_task")
        if runtime_gate:
            expected_stages.update({"tool_grant", "tool_call"})
    grants = (
        await db.execute(select(E2AGToolGrant).where(E2AGToolGrant.event_log_id == event_log.id))
    ).scalars().all()
    approvals = (
        await db.execute(select(E2AGApproval).where(E2AGApproval.event_log_id == event_log.id))
    ).scalars().all()
    effect_kind = SCENARIOS[scenario]["effect"]
    return {
        "scenario": scenario,
        "run": index,
        "pre_gate": int(pre_gate),
        "runtime_gate": int(runtime_gate),
        "event_status": event_log.status,
        "task_created": int(task is not None),
        "approval_created": int(bool(approvals)),
        "grant_created": int(bool(grants)),
        "response_status": response_status or 0,
        "upstream_reached": int(upstream_reached),
        "allowed_effect": int(upstream_reached and effect_kind == "allowed"),
        "forbidden_effect": int(upstream_reached and effect_kind == "forbidden"),
        "audit_valid": int(verify_audit_chain(audit)),
        "trace_complete": int(expected_stages.issubset(set(stages))),
        "trace_id": event_log.trace_id,
        "audit_stages": ">".join(stages),
        "error": error or "",
    }


def aggregate(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        config = f'G{row["pre_gate"]}R{row["runtime_gate"]}'
        grouped[(config, row["scenario"])].append(row)
    result = []
    for (config, scenario), values in sorted(grouped.items()):
        result.append({
            "config": config,
            "scenario": scenario,
            "runs": len(values),
            "tasks_created": sum(row["task_created"] for row in values),
            "approvals_created": sum(row["approval_created"] for row in values),
            "grants_created": sum(row["grant_created"] for row in values),
            "upstream_calls": sum(row["upstream_reached"] for row in values),
            "allowed_effects": sum(row["allowed_effect"] for row in values),
            "forbidden_effects": sum(row["forbidden_effect"] for row in values),
            "audit_valid": sum(row["audit_valid"] for row in values),
            "trace_complete": sum(row["trace_complete"] for row in values),
            "response_statuses": sorted({row["response_status"] for row in values}),
            "sample_audit_stages": values[0]["audit_stages"],
        })
    return result


async def experiment(repeats: int) -> dict:
    old_mode = settings.e2ag_mode
    old_dedup = settings.event_dedup_enabled
    old_base = settings.e2ag_internal_base_url
    settings.event_dedup_enabled = False
    settings.e2ag_internal_base_url = "http://experiment.local"
    CanaryAsyncClient.calls = []
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    rows: list[dict] = []
    try:
        async with sessions() as db:
            agent = Agent(
                id="e2e-agent",
                name="Deterministic E2E Agent",
                mode="task",
                capabilities={"governance": {
                    "allowed_event_sources": ["github/team-alpha/*"],
                    "allowed_event_types": ["git.push"],
                    "allowed_actions": ["git.read", "secret.read"],
                    "allowed_tools": ["ledger.record_*"],
                    "require_action_declaration": True,
                }},
            )
            server = McpServer(
                id="e2e-mcp",
                name="canary",
                transport="streamable_http",
                url="https://canary.invalid/mcp",
                headers={"X-Upstream-Key": "experiment-secret"},
            )
            db.add_all([agent, server])
            await db.commit()
            run_id = 0
            for _config, pre_gate, runtime_gate in CONFIGS:
                for scenario in SCENARIOS:
                    for _ in range(repeats):
                        run_id += 1
                        rows.append(await run_once(
                            db, agent, server, scenario, run_id, pre_gate, runtime_gate,
                        ))
        summary = aggregate(rows)
        return {
            "protocol": "event-agent-tool-chain-v1",
            "repeats_per_scenario_config": repeats,
            "scenarios": list(SCENARIOS),
            "configurations": {
                name: {"pre_dispatch_pep": pre, "runtime_mcp_pep": runtime}
                for name, pre, runtime in CONFIGS
            },
            "executions": len(rows),
            "summary": summary,
            "rows": rows,
            "limitations": [
                "Real DiOS dispatcher/DB/A2ATask/ToolGrant/MCP PEP path with deterministic Agent and MCP test double.",
                "No LLM, container runtime, external TCP network, or production credentials are included.",
                "The experiment measures whether a forbidden upstream effect is reachable, not semantic attack detection.",
            ],
        }
    finally:
        settings.e2ag_mode = old_mode
        settings.event_dedup_enabled = old_dedup
        settings.e2ag_internal_base_url = old_base
        await engine.dispose()


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--output", type=Path, default=HERE / "results")
    args = parser.parse_args()
    result = await experiment(args.repeats)
    args.output.mkdir(parents=True, exist_ok=True)
    rows = result.pop("rows")
    (args.output / "e2e_chain_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (args.output / "e2e_chain_runs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(async_main())
