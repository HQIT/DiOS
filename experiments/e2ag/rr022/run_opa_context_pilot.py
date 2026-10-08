"""E2AG vs E2AG-with-OPA: shared host, independently evaluated call-boundary PDP.

No event admission/approval implementation or performance claims. The OPA path
does not call native grant/argument predicates. It uses the same database,
credential authentication, state effects, audit and inert MCP receipt endpoint.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
from datetime import timedelta
import importlib.metadata
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import traceback
from unittest.mock import patch

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, HERE, digest, file_hash
from run_parameter_pilot import constraints, jobs as parameter_jobs
from run_state_pilot import NOW, e2ag_fixture, grant_and_connection, invoke_proxy

OPA = ROOT / "tmp/external-baselines/opa-v1.17.0/opa_linux_amd64_static"
OPA_SHA = "e83da46804832578e9d9e1733dffbe4d3b5f8cc9c26eb124da9ceea4abfe189f"
POLICY = HERE / "opa_call_context.rego"


def wsl_path(path):
    return subprocess.check_output(["wsl.exe", "-e", "wslpath", "-a", str(path)], text=True).strip()


class OpaAdapter:
    def __init__(self):
        if file_hash(OPA) != OPA_SHA:
            raise ValueError("Pinned OPA digest mismatch")
        self.command = ["wsl.exe", "-e", wsl_path(OPA), "eval", "--format=json", "--strict-builtin-errors", "--fail",
                        "--data", wsl_path(POLICY), "--stdin-input", "data.rr022.call_context.decision"]
        self.evaluations = []
        self.context = None

    def evaluate(self, context):
        try:
            run = subprocess.run(self.command, input=json.dumps(context), capture_output=True, text=True,
                                 encoding="utf-8", timeout=20)
            if run.returncode:
                raise RuntimeError(f"OPA failed ({run.returncode}): {run.stderr}")
            result = json.loads(run.stdout)["result"][0]["expressions"][0]["value"]
            if set(result) != {"allow", "reason"} or type(result["allow"]) is not bool or not isinstance(result["reason"], str):
                raise ValueError("Unexpected OPA result shape")
            error = None
        except Exception as exc:
            result, error = {"allow": False, "reason": "OPA_EVAL_ERROR"}, f"{type(exc).__name__}: {exc}"
        self.evaluations.append({"input": copy.deepcopy(context), "result": result, "error": error})
        return result

    async def validate(self, db, grant_id, raw_token):
        from app.models.tables import A2ATask, E2AGToolGrant, EventLog
        from app.services.e2ag_tool_gateway import hash_grant_token, _aware, _now, _append_event_audit
        grant = await db.get(E2AGToolGrant, grant_id)
        # Shared host credential authentication, not an OPA policy verdict.
        if grant is None or not raw_token or not secrets.compare_digest(grant.token_hash, hash_grant_token(raw_token)):
            return None, "TOOL_GRANT_INVALID"
        task = await db.get(A2ATask, grant.task_id)
        event = await db.get(EventLog, grant.event_log_id)
        ctx = {"phase": "grant", "authenticated": True, "now": _now().timestamp(),
               "grant": {key: copy.deepcopy(getattr(grant, key)) for key in [
                   "id", "task_id", "agent_id", "trace_id", "event_log_id", "mcp_server_id", "status", "allowed_tools", "argument_constraints"]},
               "task": {key: getattr(task, key) for key in ["id", "agent_id", "trace_id", "status"]} if task else None,
               "event": {key: getattr(event, key) for key in ["id", "source", "event_type", "trace_id", "contract_decision", "policy_decision"]} if event else None}
        ctx["grant"]["expires_at"] = _aware(grant.expires_at).timestamp()
        self.context = ctx
        result = self.evaluate(ctx)
        reason = result["reason"]
        # Same host-side transition effects as native validate_grant; the reason
        # is produced by Rego. These effects are not credited to OPA itself.
        if reason in {"TOOL_GRANT_EXPIRED", "TOOL_GRANT_TASK_TERMINAL"}:
            grant.status = "expired" if reason == "TOOL_GRANT_EXPIRED" else "revoked"
            evidence = {"grant_id": grant.id, "task_id": grant.task_id}
            if reason == "TOOL_GRANT_TASK_TERMINAL":
                evidence.update(reason="task_terminal", task_status=task.status)
            await _append_event_audit(db, grant.event_log_id, trace_id=grant.trace_id, stage="tool_grant",
                                      outcome="grant_expired" if grant.status == "expired" else "grant_revoked", evidence=evidence)
            await db.commit()
        return (grant if result["allow"] else None), reason

    def authorize(self, method, params, allowed_tools, argument_constraints=None):
        if self.context is None:
            raise RuntimeError("OPA authorization without authenticated stored context")
        ctx = copy.deepcopy(self.context)
        assert ctx["grant"]["allowed_tools"] == allowed_tools
        assert ctx["grant"]["argument_constraints"] == argument_constraints
        args = params.get("arguments")
        ctx.update(phase="call", call={"method": method, "name": params.get("name"), "arguments": args},
                   argument_types={k: type(v).__name__ for k, v in args.items()} if isinstance(args, dict) else None)
        result = self.evaluate(ctx)
        return result["allow"], result["reason"]


async def run_method(job, method):
    async with e2ag_fixture(job) as (db, agent, server, receipts):
        from app.models.tables import A2ATask, EventLog
        from app.services.e2ag_tool_gateway import revoke_task_grants
        from app.services.e2ag import verify_audit_chain
        data = job["input"]
        agent.capabilities = {"governance": {"allowed_tools": job["policy"]["allowed_tools"],
                                             "argument_constraints": constraints(job["policy"])}}
        event = EventLog(id=data["event"]["id"], source=data["event"]["source"], event_type=data["event"]["type"],
                         cloud_event=data["event"], trace_id=data["grant"]["trace_id"], status="dispatched")
        task_data = {**data["task"], "trace_id": data["grant"]["trace_id"]}
        task = A2ATask(**task_data, context_id=event.id, message={"fixture": True})
        db.add_all([event, task])
        await db.commit()
        grant, conn = await grant_and_connection(db, agent, server, task)
        grant.expires_at = NOW + timedelta(seconds=data["grant"]["expires_at"] - data["host"]["now"])
        task.trace_id = data["task"]["trace_id"]
        await db.commit()
        if data["grant"]["status"] == "revoked":
            await revoke_task_grants(db, task.id)
        await db.refresh(event)
        before = copy.deepcopy(event.audit_chain)
        adapter = OpaAdapter() if method == "opa" else None
        with contextlib.ExitStack() as stack:
            if adapter:
                stack.enter_context(patch("app.api.internal.e2ag_mcp.validate_grant", adapter.validate))
                stack.enter_context(patch("app.api.internal.e2ag_mcp.authorize_mcp_request", adapter.authorize))
                # Tripwires: accidentally delegating a policy verdict to E2AG
                # must fail the worker, never silently validate the baseline.
                for target in ["app.services.e2ag_tool_gateway.validate_grant",
                               "app.services.e2ag_tool_gateway.authorize_mcp_request",
                               "app.services.e2ag_tool_gateway.check_arguments"]:
                    stack.enter_context(patch(target, side_effect=AssertionError("Native policy predicate invoked in OPA path")))
            response = await invoke_proxy(db, grant, conn, data["call"])
        await db.refresh(event)
        await db.refresh(grant)
        calls = [e for e in event.audit_chain[len(before):] if e["stage"] == "tool_call"]
        engine_errors = [e["error"] for e in adapter.evaluations if e["error"]] if adapter else []
        return {"decision": "allow" if response["status"] == 200 else "deny", "response": response,
                "tool_receipts": receipts, "input_sha256": digest(job), "grant_status": grant.status,
                "grant_call_count": grant.call_count, "audit_added": event.audit_chain[len(before):],
                "audit_verification": verify_audit_chain(event.audit_chain), "call_audit_count": len(calls),
                "opa_evaluations": adapter.evaluations if adapter else [], "engine_errors": engine_errors,
                "stored_constraints": grant.argument_constraints}


def jobs(spec):
    yield from parameter_jobs(spec)
    for case in spec["cases"]:
        if not case["id"].startswith("L"):
            continue
        data = copy.deepcopy(spec["base_input"])
        data["approval"].pop("answer", None)
        for key, value in case["overrides"].items():
            target = data
            parts = key.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        yield case["id"], {"policy": spec["policy"], "input": data}, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["e2ag", "opa"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        job = json.load(sys.stdin)
        assert set(job) == {"policy", "input"}
        stream = io.StringIO()
        try:
            with contextlib.redirect_stdout(stream):
                result = asyncio.run(run_method(job, args.worker))
            result["error"] = None
        except Exception:
            result = {"error": traceback.format_exc()}
        result.update(stdout=stream.getvalue(), model_calls=0)
        print(json.dumps(result, ensure_ascii=False))
        return
    if not args.output or args.output.exists():
        raise ValueError("New output required")
    assert file_hash(OPA) == OPA_SHA
    spec_path = HERE / "common-cases-v0.1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    rows = []
    for case_id, job, count in jobs(spec):
        for method in ("e2ag", "opa"):
            proc = subprocess.run([str(ROOT / "backend/.venv/Scripts/python.exe"), str(Path(__file__).resolve()), "--worker", method],
                                  input=json.dumps(job), capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=90)
            try:
                observed = json.loads(proc.stdout)
            except ValueError:
                observed = {"error": "Invalid worker output", "stdout": proc.stdout}
            passed = (proc.returncode == 0 and not observed.get("error") and not observed.get("engine_errors")
                      and observed.get("input_sha256") == digest(job) and observed.get("audit_verification") is True
                      and observed.get("call_audit_count") == 1
                      and observed.get("decision") == ("allow" if count else "deny")
                      and len(observed.get("tool_receipts", [])) == count)
            rows.append({"case": case_id, "control": case_id.startswith("C_"), "method": method, "job": job,
                         "expected_receipts": count, "observed": observed, "pass": passed,
                         "exit_code": proc.returncode, "stderr": proc.stderr})
            print(json.dumps({"case": case_id, "method": method, "pass": passed, "error": observed.get("error"),
                              "engine_errors": observed.get("engine_errors"), "decision": observed.get("decision")}), flush=True)
    source_paths = [Path(__file__), POLICY, HERE / 'run_parameter_pilot.py', HERE / 'run_state_pilot.py',
                    ROOT / 'backend/app/services/e2ag_tool_gateway.py', ROOT / 'backend/app/services/e2ag_arguments.py',
                    ROOT / 'backend/app/api/internal/e2ag_mcp.py', ROOT / 'backend/app/models/tables.py']
    report = {"protocol": "rr022-opa-call-context-v1", "rows": rows,
              "opa_binary_sha256": file_hash(OPA), "opa_version": subprocess.check_output(["wsl.exe", "-e", wsl_path(OPA), "version"], text=True),
              "cases_sha256": file_hash(spec_path), "source_sha256": {str(p.relative_to(ROOT)): file_hash(p) for p in source_paths},
              "passed": sum(r["pass"] for r in rows), "total": len(rows),
              "scope": "Call-boundary context: credential-authenticated native task/grant snapshot, lifecycle, tool/argument predicates.",
              "not_covered": ["event admission/source authentication", "approval state machine", "concurrent revoke-check-use atomicity",
                              "real independent workload", "network deployment and performance", "all fnmatch/glob dialects"]}
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"{report['passed']}/{report['total']} -> {args.output}")
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    main()
