"""Approval/lifecycle interface probes. No LLM or external service calls.

Approval answers travel on a separate pipe only after the native request.
Persisted E2AG approval and synchronous AgentSpec inspection are not equated.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import queue
import subprocess
import sys
import threading
import time
import traceback
from unittest.mock import AsyncMock, patch

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, HERE, UPSTREAM, PIN, CATALOG, digest, file_hash, receipt

PIPE_OUT = sys.stdout
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def emit(value):
    PIPE_OUT.write(json.dumps(value, ensure_ascii=False) + "\n")
    PIPE_OUT.flush()


def request_answer(snapshot):
    emit({"type": "approval_requested", **snapshot})
    message = json.loads(sys.stdin.readline())
    if set(message) != {"answer"} or message["answer"] not in {"yes", "no"}:
        raise ValueError("Expected an answer after the native approval request")
    return message["answer"]


@contextlib.asynccontextmanager
async def e2ag_fixture(job):
    sys.path.insert(0, str(ROOT / "backend"))
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.config import settings
    from app.db.database import Base
    from app.models.tables import Agent, McpServer
    data = job["input"]
    receipts = []

    class MemoryMCP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            assert url == "https://receipt.invalid/mcp"
            assert json["method"] == "tools/call"
            item = receipt(receipts, json["params"]["name"], json["params"]["arguments"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": json["id"], "result": item})

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    old = (settings.e2ag_mode, settings.event_dedup_enabled)
    settings.e2ag_mode, settings.event_dedup_enabled = "enforce", False
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            agent = Agent(id=data["task"]["agent_id"], name="Inert state fixture", mode="task",
                          capabilities={"governance": {
                              "allowed_event_sources": list(job["policy"]["source_contract"]),
                              "allowed_event_types": sorted({t for ts in job["policy"]["source_contract"].values() for t in ts}),
                              "allowed_actions": ["admin.report", "report.create"],
                              "allowed_tools": job["policy"]["allowed_tools"], "require_action_declaration": True}})
            server = McpServer(id="mcp-fixture", name="receipt", transport="streamable_http", url="https://receipt.invalid/mcp")
            db.add_all([agent, server])
            await db.commit()
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", MemoryMCP), \
                 patch("app.services.e2ag_tool_gateway._now", return_value=NOW), \
                 patch("app.services.e2ag_approval._now", return_value=NOW):
                yield db, agent, server, receipts
    finally:
        settings.e2ag_mode, settings.event_dedup_enabled = old
        await engine.dispose()


async def grant_and_connection(db, agent, server, task):
    from app.services.e2ag_tool_gateway import issue_task_mcp_config
    from app.models.tables import E2AGToolGrant
    config, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
    if omissions:
        raise RuntimeError(str(omissions))
    conn = config["receipt"]
    grant = await db.get(E2AGToolGrant, conn["url"].rsplit("/", 1)[-1])
    return grant, conn


async def invoke_proxy(db, grant, conn, call):
    from app.api.internal.e2ag_mcp import proxy_mcp_call
    from starlette.requests import Request
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": call["tool"], "arguments": call["arguments"]}}

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/fixture",
                       "headers": [(b"authorization", conn["headers"]["Authorization"].encode())]}, receive)
    response = await proxy_mcp_call(grant.id, request, db)
    return {"status": response.status_code, "body": json.loads(response.body)}


async def e2ag_approval(job):
    async with e2ag_fixture(job) as (db, agent, server, receipts):
        from sqlalchemy import select
        from app.connectors import registry
        from app.connectors.contracts import ConnectorManifest, EventTypeDecl, EventNamespaceDecl
        from app.models.tables import A2ATask, E2AGApproval
        from app.services import a2a_service
        from app.services.event_dispatcher import dispatch_event
        from app.services.e2ag_approval import decide_approval
        from app.services.e2ag import verify_audit_chain
        registry.all_manifests()
        registry.register(ConnectorManifest(type="rr022-fixture", label="Inert fixture", instantiable=False,
                          event_namespaces=tuple(EventNamespaceDecl(source, tuple(EventTypeDecl(t) for t in types))
                                                 for source, types in job["policy"]["source_contract"].items())))
        data = job["input"]
        native_event = {**data["event"], "specversion": "1.0", "data": {
            "requested_action": "admin.report" if data["approval"]["required"] else "report.create",
            "requested_tool": data["call"]["tool"], "environment": "production",
            "target_resource": data["call"]["arguments"]["resource"]}}

        async def fixed_send(**kwargs):
            return await a2a_service.create_task(**kwargs)

        with patch("app.services.a2a_service.send_message", AsyncMock(side_effect=fixed_send)):
            event, detail = await dispatch_event(native_event, [agent.id], db)
            if event is None:
                raise RuntimeError(f"Missing event: {detail}")
            approval = (await db.execute(select(E2AGApproval).where(E2AGApproval.event_log_id == event.id))).scalar_one()
            tasks_before = (await db.execute(select(A2ATask))).scalars().all()
            snapshot = {"method": "e2ag", "native_state": approval.status, "event_status": event.status,
                        "receipt_count": len(receipts), "created_tasks": len(tasks_before),
                        "audit_chain": copy.deepcopy(event.audit_chain), "input_sha256": digest(job),
                        "semantics": "native durable pending state; harness waits for separate response"}
            answer = request_answer(snapshot)
            approval, event = await decide_approval(db, approval.id, decision="approved" if answer == "yes" else "rejected",
                                                    actor="fixture-human", reason="scripted after native request")
        tasks = (await db.execute(select(A2ATask))).scalars().all()
        response = None
        for task in tasks:
            grant, conn = await grant_and_connection(db, agent, server, task)
            response = await invoke_proxy(db, grant, conn, data["call"])
        # Only native resume creates tasks; no task is synthesized from yes/no here.
        await db.refresh(event)
        duplicate = None
        before_duplicate = len(tasks)
        try:
            await decide_approval(db, approval.id, decision="approved", actor="fixture-human")
        except ValueError as exc:
            duplicate = str(exc)
        task_count_after = len((await db.execute(select(A2ATask))).scalars().all())
        return {"decision_sequence": ["approval_required", "allow" if approval.status == "approved" else "deny"],
                "native_state": approval.status, "native_event": native_event, "tool_receipts": receipts,
                "created_tasks": len(tasks), "response": response, "audit_chain": event.audit_chain,
                "audit_verification": verify_audit_chain(event.audit_chain),
                "duplicate_reply": {"error": duplicate, "tasks_before": before_duplicate, "tasks_after": task_count_after},
                "pre_answer": snapshot, "input_sha256": digest(job)}


async def e2ag_lifecycle(job):
    async with e2ag_fixture(job) as (db, agent, server, receipts):
        from app.models.tables import A2ATask, EventLog
        from app.services.e2ag_tool_gateway import revoke_task_grants
        from app.services.e2ag import verify_audit_chain
        data = job["input"]
        event = EventLog(id=data["event"]["id"], source=data["event"]["source"], event_type=data["event"]["type"],
                         cloud_event=data["event"], trace_id=data["grant"]["trace_id"], status="dispatched")
        initial_task = {**data["task"], "trace_id": data["grant"]["trace_id"], "agent_id": data["grant"]["agent_id"]}
        task = A2ATask(**initial_task, context_id=event.id, message={"fixture": True})
        db.add_all([event, task])
        await db.commit()
        grant, conn = await grant_and_connection(db, agent, server, task)
        grant.expires_at = NOW + timedelta(seconds=data["grant"]["expires_at"] - data["host"]["now"])
        await db.commit()
        mutations = []
        if data["grant"]["status"] == "revoked":
            await revoke_task_grants(db, task.id)
            mutations.append("native revoke_task_grants before invocation")
        task.trace_id = data["task"]["trace_id"]
        task.agent_id = data["task"]["agent_id"]
        await db.commit()
        await db.refresh(event)
        before = copy.deepcopy(event.audit_chain)
        response = await invoke_proxy(db, grant, conn, data["call"])
        await db.refresh(event)
        await db.refresh(grant)
        return {"decision_sequence": ["deny" if response["status"] == 403 else "allow"],
                "response": response, "tool_receipts": receipts, "grant_status": grant.status,
                "grant_task_binding": {"task_trace": task.trace_id, "grant_trace": grant.trace_id},
                "grant_call_count": grant.call_count, "state_operations": mutations,
                "audit_before_call": before, "audit_chain": event.audit_chain,
                "audit_added_by_call": event.audit_chain[len(before):],
                "audit_verification": verify_audit_chain(event.audit_chain), "input_sha256": digest(job)}


def agentspec_approval(job):
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=UPSTREAM, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=UPSTREAM, text=True)
    if sha != PIN or any(not (s.endswith(".pyc") and "__pycache__/" in s) for s in dirty.splitlines()):
        raise RuntimeError("Expected unmodified pinned AgentSpec")
    sys.path.insert(0, str(UPSTREAM / "src"))
    from rule import Rule
    from langchain.agents.agent import BaseSingleActionAgent
    from langchain_core.agents import AgentAction, AgentFinish
    from langchain_core.tools import Tool
    from controlled_agent_excector import ControlledAgentExecutor
    compat_path = HERE / "run_agentspec_smoke.py"
    node = next(n for n in ast.walk(ast.parse(compat_path.read_text(encoding="utf-8")))
                if isinstance(n, ast.ClassDef) and n.name == "FinishCompatibleRule")
    scope = {"Rule": Rule}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(compat_path), "exec"), scope)

    class Planner(BaseSingleActionAgent):
        @property
        def input_keys(self):
            return ["input"]

        def plan(self, intermediate_steps, callbacks=None, **kwargs):
            if intermediate_steps:
                return AgentFinish({"output": "done"}, "fixed completion after one action/skip")
            call = kwargs["input"]["call"]
            return AgentAction(CATALOG[call["tool"]], json.dumps(call["arguments"], sort_keys=True), "fixed call")

        async def aplan(self, intermediate_steps, callbacks=None, **kwargs):
            return self.plan(intermediate_steps, callbacks, **kwargs)

    receipts, prompts = [], []
    tools = [Tool(name=alias, description="Inert receipt", func=lambda value, name=name: json.dumps(receipt(receipts, name, json.loads(value))))
             for name, alias in CATALOG.items()]
    rules_text = []
    for name, alias in CATALOG.items():
        rules_text.append(f"rule @catalog_{alias}\ntrigger {alias}\ncheck "
                          f"{'false' if name in job['policy']['allowed_tools'] else 'true'}\nenforce stop\nend")
    for name, alias in CATALOG.items():
        rules_text.append(f"rule @approval_{alias}\ntrigger {alias}\ncheck "
                          f"{'true' if job['input']['approval']['required'] else 'false'}\nenforce user_inspection\nend")
    rules = [scope["FinishCompatibleRule"](**Rule.from_text(s).model_dump()) for s in rules_text]
    executor = ControlledAgentExecutor.from_agent_and_tools(Planner(), tools, rules, max_iterations=2, verbose=False)

    def human(prompt):
        snap = {"method": "agentspec", "native_prompt": prompt, "receipt_count": len(receipts),
                "input_sha256": digest(job), "semantics": "native synchronous UserInspection input is blocked"}
        prompts.append(snap)
        return request_answer(snap)

    with patch("builtins.input", human):
        result = executor.invoke({"input": job["input"]})
    observations = [str(observation) for _, observation in result["intermediate_steps"]]
    skipped = any("action is skipped by user" in value for value in observations)
    final_decision = "deny" if skipped else "allow" if receipts else "unclassified"
    return {"decision_sequence": ["approval_required", final_decision], "tool_receipts": receipts,
            "native_output": result["output"], "native_observations": observations,
            "native_rule_texts": rules_text, "pre_answer": prompts[0], "prompt_count": len(prompts),
            "finish_compat": True, "compat_source_sha256": file_hash(compat_path),
            "input_sha256": digest(job), "upstream_commit": sha,
            "semantics": "deny means this action skipped, not a persisted rejected task"}


def worker(method, cohort):
    job = json.loads(sys.stdin.readline())
    if set(job) != {"input", "policy"} or "answer" in job["input"]["approval"]:
        raise ValueError("Answers/oracle/case IDs must not be in initial worker input")
    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            if method == "agentspec":
                result = agentspec_approval(job)
            else:
                result = asyncio.run(e2ag_approval(job) if cohort == "approval" else e2ag_lifecycle(job))
        result["error"] = None
    except Exception:
        result = {"error": traceback.format_exc()}
    result.update({"stdout": console.getvalue(), "python": platform.python_version(), "model_calls": 0,
                   "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}})
    emit({"type": "result", "observed": result})


def run_worker(method, cohort, job, answer):
    python = ROOT / ("backend/.venv/Scripts/python.exe" if method == "e2ag" else "tmp/rr022-baselines/.venv-agentspec/Scripts/python.exe")
    proc = subprocess.Popen([str(python), str(Path(__file__).resolve()), "--worker", method, "--cohort", cohort],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", cwd=ROOT, bufsize=1)
    messages, stderr, trace = queue.Queue(), [], []

    def reader():
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                messages.put({"type": "invalid_output", "line": line})
        messages.put({"type": "eof"})

    readers = [threading.Thread(target=reader, daemon=True),
               threading.Thread(target=lambda: stderr.extend(proc.stderr.readlines()), daemon=True)]
    for thread in readers:
        thread.start()
    try:
        proc.stdin.write(json.dumps(job) + "\n")
        proc.stdin.flush()
        while True:
            message = messages.get(timeout=45)
            trace.append(message)
            if message["type"] == "approval_requested":
                if answer is None:
                    time.sleep(0.25)  # observation window only, never a latency measurement
                    alive = proc.poll() is None and messages.empty()
                    return {"decision_sequence": ["approval_required"], "tool_receipts": [],
                            "pre_answer": message, "pending_observed": alive,
                            "observation_only": True, "input_sha256": message.get("input_sha256"),
                            "termination": "harness terminates after pending checkpoint; not a rejection or failure",
                            "error": None}, trace, stderr
                proc.stdin.write(json.dumps({"answer": answer}) + "\n")
                proc.stdin.flush()
                trace.append({"type": "harness_answer_delivered", "answer": answer})
            elif message["type"] == "result":
                proc.wait(timeout=5)
                return message["observed"], trace, stderr
            else:
                raise RuntimeError(str(message))
    except Exception:
        return {"error": traceback.format_exc()}, trace, stderr
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        for thread in readers:
            thread.join(timeout=2)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["e2ag", "agentspec"])
    parser.add_argument("--cohort", choices=["approval", "lifecycle"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.cohort)
    if args.output is None or args.output.exists():
        raise ValueError("Supply a fresh result filename")
    spec_path = HERE / "common-cases-v0.1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    rows = []
    for case in spec["cases"]:
        if case["cohort"] not in {"approval", "lifecycle"}:
            continue
        data = copy.deepcopy(spec["base_input"])
        for key, value in case["overrides"].items():
            target = data
            parts = key.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        answer = data["approval"].pop("answer")
        job = {"policy": spec["policy"], "input": data}
        methods = ["e2ag", "agentspec"] if case["cohort"] == "approval" else ["e2ag"]
        for method in methods:
            observed, trace, stderr = run_worker(method, case["cohort"], job, answer)
            oracle = case["oracle"]
            passed = (observed.get("error") is None and observed.get("decision_sequence") == oracle["decision_sequence"]
                      and len(observed.get("tool_receipts", [])) == oracle["receipt_count"]
                      and observed.get("input_sha256") == digest(job))
            if case["cohort"] == "approval":
                passed = passed and observed.get("pre_answer", {}).get("receipt_count") == 0
                if answer is None:
                    passed = passed and observed.get("pending_observed") is True
                if method == "e2ag":
                    passed = passed and observed.get("pre_answer", {}).get("created_tasks") == 0
            rows.append({"case": case["id"], "method": method, "job": job, "harness_answer": answer,
                         "expected": oracle, "observed": observed, "pipe_trace": trace, "stderr": stderr, "pass": bool(passed)})
            print(json.dumps({"case": case["id"], "method": method, "pass": bool(passed),
                              "sequence": observed.get("decision_sequence"), "error": observed.get("error")}, ensure_ascii=False), flush=True)
    source_paths = [ROOT / "backend/app/services" / name for name in ["e2ag.py", "e2ag_approval.py", "e2ag_tool_gateway.py", "event_dispatcher.py", "a2a_service.py"]]
    source_paths += [ROOT / "backend/app/api/internal/e2ag_mcp.py", ROOT / "backend/app/models/tables.py"]
    report = {"protocol": "rr022-state-pilot-v1", "rows": rows, "passed": sum(r["pass"] for r in rows), "total": len(rows),
              "cases_sha256": file_hash(spec_path), "harness_sha256": file_hash(Path(__file__)),
              "shared_helpers_sha256": file_hash(HERE / "run_paired_tool_pilot.py"),
              "e2ag_source_sha256": {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p) for p in source_paths},
              "agentspec_source_sha256": {str(p.relative_to(UPSTREAM)).replace('\\', '/'): file_hash(p) for p in sorted((UPSTREAM / 'src').rglob('*.py'))},
              "coverage_gaps": ["Agent libOS lifecycle pairing remains pending; not counted as failure or successful denial.",
                                "AgentSpec durable lifecycle is not implemented by a shared surrogate predicate.",
                                "Only valid arguments used; parameter/resource enforcement remains untested.",
                                "Pending snapshot is followed by harness termination; no unbounded waiting or timeout-as-denial."],
              "limits": "Six handcrafted cases, nine method-case runs. Synthetic runtime and in-memory MCP. No model, real endpoint or performance claim."}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(str(args.output))
    if not all(r["pass"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    main()
