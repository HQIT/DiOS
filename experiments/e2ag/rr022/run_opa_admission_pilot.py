"""Independent Rego admission PDP + shared native durable approval/execution.

Only declared fixture fields are supported; not a complete port of all native
policy syntaxes. No credential/source-authentication claim (E01 stays pending).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback
from unittest.mock import AsyncMock, patch

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, HERE, file_hash, digest
from run_state_pilot import e2ag_fixture, grant_and_connection, invoke_proxy, request_answer
from run_opa_context_pilot import OPA, OPA_SHA, wsl_path
from run_opa_audited_pilot import AuditedOpaAdapter
from run_parameter_pilot import constraints

REG0 = HERE / "opa_admission.rego"


class AdmissionAdapter:
    def __init__(self):
        assert file_hash(OPA) == OPA_SHA
        self.command = ["wsl.exe", "-e", wsl_path(OPA), "eval", "--format=json", "--strict-builtin-errors", "--fail",
                        "--data", wsl_path(REG0), "--stdin-input", "data.rr022.admission.result"]
        self.evaluations = []

    def evaluate(self, event, targets):
        from app.services.e2ag import _contract_bindings, SENSITIVE_ACTION_PREFIXES
        data = {"event": copy.deepcopy(event), "targets": copy.deepcopy(targets),
                "bindings": [{"contract_type": t, "pattern": p, "event_type": e} for t, p, e in _contract_bindings()],
                "sensitive_prefixes": list(SENSITIVE_ACTION_PREFIXES)}
        run = subprocess.run(self.command, input=json.dumps(data), capture_output=True, text=True, encoding="utf-8", timeout=20)
        if run.returncode:
            raise RuntimeError(f"Admission OPA failure: {run.stderr}")
        result = json.loads(run.stdout)["result"][0]["expressions"][0]["value"]
        self.evaluations.append({"input": data, "result": result})
        return result

    def decision(self, stage, value):
        from app.services.e2ag import Decision
        assert value["decision"] in {"allow", "deny", "approval_required"}
        return Decision(stage=stage, decision=value["decision"], reason_codes=(value["reason"],),
                        policy_version="rr022-opa-admission-v1", evidence={"rego_sha256": file_hash(REG0)})

    def contract(self, event):
        return self.decision("contract", self.evaluate(event, {})["contract"])

    def policy(self, event, contract, targets=None):
        # Re-evaluate contract from raw declarations, never accept a Python
        # precomputed contract verdict as the policy result.
        return self.decision("policy", self.evaluate(event, targets or {})["policy"])


async def execute(job, method):
    async with e2ag_fixture(job) as (db, agent, server, receipts):
        from sqlalchemy import select
        from app.connectors import registry
        from app.connectors.contracts import ConnectorManifest, EventNamespaceDecl, EventTypeDecl
        from app.models.tables import A2ATask, E2AGApproval
        from app.services import a2a_service
        from app.services.event_dispatcher import dispatch_event
        from app.services.e2ag_approval import decide_approval
        from app.services.e2ag import verify_audit_chain
        registry.all_manifests()
        registry.register(ConnectorManifest(type="rr022-fixture", label="Inert fixture", instantiable=False,
                          event_namespaces=tuple(EventNamespaceDecl(source, tuple(EventTypeDecl(t) for t in types))
                                                 for source, types in job["policy"]["source_contract"].items())))
        caps = copy.deepcopy(agent.capabilities)
        caps["governance"]["argument_constraints"] = constraints(job["policy"])
        agent.capabilities = caps
        await db.commit()
        data = job["input"]
        native_event = {**data["event"], "specversion": "1.0", "data": {
            "requested_action": "admin.report" if data["approval"]["required"] else "report.create",
            "requested_tool": data["call"]["tool"], "environment": "production",
            "target_resource": data["call"]["arguments"]["resource"]}}

        async def fixed_send(**kwargs):
            return await a2a_service.create_task(**kwargs)

        admission = AdmissionAdapter() if method == "opa" else None
        call = AuditedOpaAdapter() if method == "opa" else None
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("app.services.a2a_service.send_message", AsyncMock(side_effect=fixed_send)))
            if admission:
                stack.enter_context(patch("app.services.event_dispatcher.evaluate_contract", admission.contract))
                stack.enter_context(patch("app.services.event_dispatcher.evaluate_policy", admission.policy))
                stack.enter_context(patch("app.api.internal.e2ag_mcp.validate_grant", call.validate))
                stack.enter_context(patch("app.api.internal.e2ag_mcp.authorize_mcp_request", call.authorize))
                for name in ("evaluate_contract", "evaluate_policy"):
                    stack.enter_context(patch("app.services.e2ag." + name, side_effect=AssertionError("Native PDP called")))
            event, detail = await dispatch_event(native_event, [agent.id], db)
            assert event is not None
            sequence = [event.policy_decision["decision"]]
            approvals = (await db.execute(select(E2AGApproval))).scalars().all()
            pre_answer, duplicate = None, None
            if approvals:
                assert len(approvals) == 1 and sequence == ["approval_required"]
                tasks = (await db.execute(select(A2ATask))).scalars().all()
                pre_answer = {"method": method, "native_state": approvals[0].status, "created_tasks": len(tasks),
                              "receipt_count": len(receipts), "audit_chain": copy.deepcopy(event.audit_chain),
                              "input_sha256": digest(job)}
                answer = request_answer(pre_answer)
                await decide_approval(db, approvals[0].id, decision="approved" if answer == "yes" else "rejected",
                                      actor="fixture-human", reason="answer delivered after pending checkpoint")
                sequence.append("allow" if answer == "yes" else "deny")
                try:
                    await decide_approval(db, approvals[0].id, decision="approved", actor="fixture-human")
                except ValueError as exc:
                    duplicate = str(exc)
            tasks = (await db.execute(select(A2ATask))).scalars().all()
            responses = []
            for task in tasks:
                grant, connection = await grant_and_connection(db, agent, server, task)
                responses.append(await invoke_proxy(db, grant, connection, data["call"]))
            await db.refresh(event)
        return {"decision_sequence": sequence, "created_tasks": len(tasks), "tool_receipts": receipts,
                "responses": responses, "approval_count": len(approvals), "pre_answer": pre_answer,
                "duplicate_reply": duplicate, "input_sha256": digest(job), "audit_chain": event.audit_chain,
                "audit_verification": verify_audit_chain(event.audit_chain), "native_event": native_event,
                "admission_evaluations": admission.evaluations if admission else [],
                "call_evaluations": call.evaluations if call else []}


def run_worker(job, method, answer):
    command = [str(ROOT / "backend/.venv/Scripts/python.exe"), str(Path(__file__).resolve()), "--worker", method]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", cwd=ROOT, bufsize=1)
    messages, stderr, trace = queue.Queue(), [], []
    def read():
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                messages.put({"type": "invalid", "line": line})
        messages.put({"type": "eof"})
    threads = [threading.Thread(target=read, daemon=True), threading.Thread(target=lambda: stderr.extend(proc.stderr.readlines()), daemon=True)]
    for thread in threads:
        thread.start()
    try:
        proc.stdin.write(json.dumps(job) + "\n")
        proc.stdin.flush()
        while True:
            message = messages.get(timeout=60)
            trace.append(message)
            if message["type"] == "approval_requested":
                if answer is None:
                    time.sleep(0.25)
                    return {"decision_sequence": ["approval_required"], "tool_receipts": [],
                            "created_tasks": message["created_tasks"], "pre_answer": message,
                            "pending_observed": proc.poll() is None and messages.empty(),
                            "input_sha256": message["input_sha256"], "error": None}, trace
                proc.stdin.write(json.dumps({"answer": answer}) + "\n")
                proc.stdin.flush()
                trace.append({"type": "harness_answer_delivered", "answer": answer})
            elif message["type"] == "result":
                proc.wait(timeout=5)
                return message["observed"], trace
            else:
                raise RuntimeError(str(message))
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["e2ag", "opa"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        job = json.loads(sys.stdin.readline())
        assert set(job) == {"policy", "input"} and "answer" not in job["input"]["approval"]
        console = io.StringIO()
        try:
            with contextlib.redirect_stdout(console):
                observed = asyncio.run(execute(job, args.worker))
            observed["error"] = None
        except Exception:
            observed = {"error": traceback.format_exc()}
        print(json.dumps({"type": "result", "observed": observed, "stdout": console.getvalue()}, ensure_ascii=False))
        return
    assert args.output and not args.output.exists()
    spec_path = HERE / "common-cases-v0.1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    selected = [c for c in spec["cases"] if c["id"] in {"T01", "E02", "A01", "A02", "A03"}]
    selected.append({"id": "C_HARD_DENY_BEFORE_APPROVAL", "overrides": {"approval.required": True, "approval.answer": "yes", "call.tool": "report.preview"},
                     "oracle": {"decision_sequence": ["deny"], "receipt_count": 0}})
    rows = []
    for case in selected:
        data = copy.deepcopy(spec["base_input"])
        for key, value in case["overrides"].items():
            target = data
            parts = key.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        answer = data["approval"].pop("answer")
        job = {"policy": spec["policy"], "input": data}
        for method in ("e2ag", "opa"):
            observed, trace = run_worker(job, method, answer)
            expected = case["oracle"]
            passed = (not observed.get("error") and observed.get("decision_sequence") == expected["decision_sequence"]
                      and len(observed.get("tool_receipts", [])) == expected["receipt_count"]
                      and observed.get("created_tasks") == expected["receipt_count"]
                      and observed.get("input_sha256") == digest(job))
            if data["approval"]["required"] and case["id"].startswith("A"):
                passed = passed and observed.get("pre_answer", {}).get("created_tasks") == 0 and observed.get("pre_answer", {}).get("receipt_count") == 0
                if answer is None:
                    passed = passed and observed.get("pending_observed") is True
                else:
                    passed = passed and observed.get("duplicate_reply") == "APPROVAL_ALREADY_DECIDED"
            if "audit_verification" in observed:
                passed = passed and observed["audit_verification"]
            rows.append({"case": case["id"], "method": method, "job": job, "expected": expected,
                         "observed": observed, "pipe_trace": trace, "pass": bool(passed)})
            print(json.dumps({"case": case["id"], "method": method, "pass": bool(passed), "error": observed.get("error")}), flush=True)
    output = {"protocol": "rr022-opa-admission-v1", "rows": rows, "passed": sum(r["pass"] for r in rows), "total": len(rows),
              "cases_sha256": file_hash(spec_path), "rego_sha256": file_hash(REG0), "harness_sha256": file_hash(Path(__file__)),
              "scope": "Five cases plus one hard-constraint control; shared durable approval and host lifecycle. Rego replaces admission/call verdicts, not the approval state machine.",
              "not_covered": ["E01 cryptographic source authentication", "all native policy syntax/aliases", "admission-engine failure audit", "approval-time policy changes", "real network/model/workload/performance"]}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    if output["passed"] != output["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    main()
