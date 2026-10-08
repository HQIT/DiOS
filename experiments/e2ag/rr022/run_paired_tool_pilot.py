"""T01/T02 calling-boundary pilot, not a full-architecture benchmark.

Run this driver with system Python. Each method runs in its isolated environment.
Only the parent sees case IDs and expected outcomes. Both real execution paths
end in the same inert receipt function; MCP HTTP is replaced by a test double.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import copy
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
UPSTREAM = ROOT / "tmp/rr022-baselines/AgentSpec"
PIN = "e6fa3902e2cfb9681f454b355691b771f70543f8"
CATALOG = {"report.create": "report_create", "report.preview": "report_preview"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def receipt(sink, tool, arguments):
    # Deliberately no policy check here: the registered tool must be executable.
    if tool not in CATALOG:
        raise ValueError("Pilot tool catalog does not contain this tool")
    item = {"tool": tool, "arguments": copy.deepcopy(arguments), "sequence": len(sink) + 1}
    sink.append(item)
    return item


def agentspec(job):
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=UPSTREAM, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=UPSTREAM, text=True)
    changes = [line for line in dirty.splitlines() if not (line.endswith(".pyc") and "__pycache__/" in line)]
    if sha != PIN or changes:
        raise RuntimeError("AgentSpec checkout must match the pinned, unmodified source")
    sys.path.insert(0, str(UPSTREAM / "src"))
    from langchain.agents.agent import BaseSingleActionAgent
    from langchain_core.agents import AgentAction, AgentFinish
    from langchain_core.tools import Tool
    from controlled_agent_excector import ControlledAgentExecutor
    from rule import Rule

    # Reuse the exact previously audited compatibility class, not a new variant.
    compat_path = HERE / "run_agentspec_smoke.py"
    tree = ast.parse(compat_path.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "FinishCompatibleRule")
    scope = {"Rule": Rule}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(compat_path), "exec"), scope)
    CompatibleRule = scope["FinishCompatibleRule"]

    class FixedPlanner(BaseSingleActionAgent):
        @property
        def input_keys(self):
            return ["input"]

        def plan(self, intermediate_steps, callbacks=None, **kwargs):
            if intermediate_steps:
                return AgentFinish({"output": "done"}, "deterministic completion")
            call = kwargs["input"]["call"]
            return AgentAction(CATALOG[call["tool"]], json.dumps(call["arguments"], sort_keys=True), "fixed call")

        async def aplan(self, intermediate_steps, callbacks=None, **kwargs):
            return self.plan(intermediate_steps, callbacks, **kwargs)

    receipts = []
    tools = [Tool(name=alias, description="In-memory receipt only",
                  func=lambda value, name=name: json.dumps(receipt(receipts, name, json.loads(value))))
             for name, alias in CATALOG.items()]
    # Static policy compilation over the complete finite catalog, before execution.
    # No case ID/oracle/call outcome participates in constructing these rules.
    rule_texts = [f"rule @catalog_{alias}\ntrigger {alias}\ncheck "
                  f"{'false' if name in job['policy']['allowed_tools'] else 'true'}\nenforce stop\nend"
                  for name, alias in CATALOG.items()]
    rules = [CompatibleRule(**Rule.from_text(text).model_dump()) for text in rule_texts]
    executor = ControlledAgentExecutor.from_agent_and_tools(
        FixedPlanner(), tools, rules, max_iterations=2, verbose=False)
    result = executor.invoke({"input": job["input"]})
    output = result.get("output", "")
    decision = "deny" if output.startswith("action stopped by rule ") else "allow" if output == "done" else "unclassified"
    return {"decision": decision, "tool_receipts": receipts, "native_output": output,
            "native_rule_texts": rule_texts, "tool_name_mapping": CATALOG,
            "finish_compat": True, "compat_source_sha256": file_hash(compat_path),
            "upstream_commit": sha, "input_sha256": digest(job),
            "source_sha256": {str(p.relative_to(UPSTREAM)).replace('\\', '/'): file_hash(p)
                              for p in sorted((UPSTREAM / 'src').rglob('*.py'))},
            "executed_interface": "ControlledAgentExecutor.invoke -> native interpreter/enforcement -> Tool.run",
            "coverage": "Static finite tool catalog only; no resource/parameter/state enforcement claimed"}


async def e2ag(job):
    sys.path.insert(0, str(ROOT / "backend"))
    import httpx
    from datetime import datetime, timezone, timedelta
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from starlette.requests import Request
    from app.api.internal.e2ag_mcp import proxy_mcp_call
    from app.config import settings
    from app.db.database import Base
    from app.models.tables import Agent, A2ATask, EventLog, McpServer, E2AGToolGrant
    from app.services.e2ag_tool_gateway import issue_task_mcp_config
    from app.services.e2ag import verify_audit_chain

    data = job["input"]
    receipts = []

    class InMemoryMCP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            if url != "https://receipt.invalid/mcp" or json["method"] != "tools/call":
                raise ValueError("Unexpected test-double request")
            item = receipt(receipts, json["params"]["name"], json["params"]["arguments"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": json["id"], "result": item})

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    old_mode = settings.e2ag_mode
    settings.e2ag_mode = "enforce"
    # Map illustrative time 1000/1100 to a stable instant, with the same TTL.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            agent = Agent(id=data["task"]["agent_id"], name="Inert fixture", mode="task",
                          capabilities={"governance": {"allowed_tools": job["policy"]["allowed_tools"]}})
            event = EventLog(id=data["event"]["id"], source=data["event"]["source"],
                             event_type=data["event"]["type"], cloud_event=data["event"],
                             trace_id=data["task"]["trace_id"], status="dispatched")
            task = A2ATask(**data["task"], context_id=event.id, message={"fixture": True})
            server = McpServer(id="mcp-fixture", name="receipt", transport="streamable_http", url="https://receipt.invalid/mcp")
            db.add_all([agent, event, task, server])
            await db.commit()
            with patch("app.services.e2ag_tool_gateway._now", return_value=now):
                config, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
                if omissions:
                    raise RuntimeError(f"Unexpected grant omissions: {omissions}")
                conn = config["receipt"]
                grant_id = conn["url"].rsplit("/", 1)[-1]
                grant = await db.get(E2AGToolGrant, grant_id)
                grant.expires_at = now + timedelta(seconds=data["grant"]["expires_at"] - data["host"]["now"])
                await db.commit()
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": data["call"]["tool"], "arguments": data["call"]["arguments"]}}

                async def receive():
                    return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

                request = Request({"type": "http", "method": "POST", "path": "/fixture",
                                   "headers": [(b"authorization", conn["headers"]["Authorization"].encode())]}, receive)
                with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", InMemoryMCP):
                    response = await proxy_mcp_call(grant_id, request, db)
            await db.refresh(event)
            await db.refresh(grant)
            entries = event.audit_chain
            last = entries[-1]
            return {"decision": last["outcome"], "tool_receipts": receipts,
                    "http_status": response.status_code, "response_body": json.loads(response.body),
                    "audit_chain": entries, "audit_verification": verify_audit_chain(entries),
                    "grant_call_count": grant.call_count, "input_sha256": digest(job),
                    "executed_interface": "native grant issuance + proxy_mcp_call (direct function invocation) -> in-memory MCP test double",
                    "source_sha256": {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p) for p in [
                        ROOT / 'backend/app/api/internal/e2ag_mcp.py',
                        ROOT / 'backend/app/services/e2ag_tool_gateway.py',
                        ROOT / 'backend/app/services/e2ag.py', ROOT / 'backend/app/models/tables.py']},
                    "coverage": "Native persisted task/grant and tool gate; event/task preloaded, no admission/HTTP transport test"}
    finally:
        settings.e2ag_mode = old_mode
        await engine.dispose()


def worker(method):
    job = json.load(sys.stdin)
    if set(job) != {"policy", "input"}:
        raise ValueError("Worker accepts only policy and input, never oracle or case ID")
    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            result = agentspec(job) if method == "agentspec" else asyncio.run(e2ag(job))
        result["error"] = None
    except Exception:
        result = {"error": traceback.format_exc()}
    result.update({"stdout": console.getvalue(), "python": platform.python_version(),
                   "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
                   "model_calls": 0})
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["e2ag", "agentspec"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if args.output is None or args.output.exists():
        raise ValueError("Supply a new output path; existing evidence must not be overwritten")
    case_path = HERE / "common-cases-v0.1.json"
    spec = json.loads(case_path.read_text(encoding="utf-8"))
    methods = {"e2ag": ROOT / "backend/.venv/Scripts/python.exe",
               "agentspec": ROOT / "tmp/rr022-baselines/.venv-agentspec/Scripts/python.exe"}
    rows = []
    for case in spec["cases"]:
        if case["id"] not in {"T01", "T02"}:
            continue
        data = copy.deepcopy(spec["base_input"])
        for key, value in case["overrides"].items():
            target = data
            parts = key.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        job = {"policy": spec["policy"], "input": data}
        for method, python in methods.items():
            proc = subprocess.run([str(python), str(Path(__file__).resolve()), "--worker", method],
                                  input=json.dumps(job), text=True, encoding="utf-8", capture_output=True,
                                  cwd=ROOT, timeout=90)
            try:
                observed = json.loads(proc.stdout)
            except ValueError:
                observed = {"error": "Invalid worker output", "stdout": proc.stdout}
            expected = case["oracle"]
            receipts = observed.get("tool_receipts", [])
            passed = (proc.returncode == 0 and not observed.get("error")
                      and observed.get("decision") == expected["decision_sequence"][0]
                      and len(receipts) == expected["receipt_count"]
                      and observed.get("input_sha256") == digest(job)
                      and all(r["tool"] == data["call"]["tool"] and r["arguments"] == data["call"]["arguments"] for r in receipts))
            rows.append({"case": case["id"], "method": method, "input": data, "expected": expected,
                         "observed": observed, "stderr": proc.stderr, "exit_code": proc.returncode, "pass": passed})
            print(json.dumps({"case": case["id"], "method": method, "pass": passed,
                              "decision": observed.get("decision"), "receipts": len(receipts)}, ensure_ascii=False), flush=True)
    controls = []
    for name in CATALOG:
        sink = []
        receipt(sink, name, spec["base_input"]["call"]["arguments"])
        controls.append({"tool": name, "direct_receipts": sink})
    report = {"protocol": "rr022-paired-tool-pilot-v1", "scope": "T01/T02 calling-boundary only",
              "policy": spec["policy"], "tool_catalog": CATALOG, "direct_tool_controls": controls,
              "harness_sha256": file_hash(Path(__file__)), "cases_sha256": file_hash(case_path),
              "repository_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "rows": rows, "passed": sum(r["pass"] for r in rows), "total": len(rows),
              "limits": ["Only two handcrafted interface cases; not a safety rate or independent workload.",
                         "No real model, remote endpoint, actual Qame event, or network performance measurement.",
                         "Both tools exist; static AgentSpec catalog rules do not establish open-world default deny.",
                         "All context is available but only tool membership is enforced in this pilot."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(str(args.output))
    if not all(row["pass"] for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    main()
