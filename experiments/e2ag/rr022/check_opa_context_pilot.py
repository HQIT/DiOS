"""Evidence checks and separately reported OPA adapter fault probes."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.dont_write_bytecode = True
from run_opa_context_pilot import OpaAdapter, run_method, ROOT, POLICY, wsl_path, OPA
from run_paired_tool_pilot import file_hash, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assert not args.output.exists()
    report = json.loads(args.result.read_text(encoding="utf-8"))
    rows = {(r["case"], r["method"]): r for r in report["rows"]}
    assert len(rows) == 36 and report["passed"] == 36
    for relative, sha in report["source_sha256"].items():
        assert file_hash(ROOT / relative) == sha
    for case in {key[0] for key in rows}:
        a, b = rows[case, "e2ag"], rows[case, "opa"]
        assert a["job"] == b["job"] and a["pass"] and b["pass"]
        ao, bo = a["observed"], b["observed"]
        assert ao["decision"] == bo["decision"]
        assert ao["tool_receipts"] == bo["tool_receipts"]
        assert ao["grant_status"] == bo["grant_status"]
        assert ao["grant_call_count"] == bo["grant_call_count"] == 1
        assert ao["audit_verification"] and bo["audit_verification"]
        assert ao["input_sha256"] == bo["input_sha256"] == digest(a["job"])
        for item in bo["tool_receipts"]:
            assert item["arguments"] == b["job"]["input"]["call"]["arguments"]
            assert item["tool"] == b["job"]["input"]["call"]["tool"]
        ev = bo["opa_evaluations"]
        assert len(ev) == (1 if case.startswith("L") else 2)
        assert all(not e["error"] for e in ev)
        assert ev[0]["input"]["phase"] == "grant"
        for e in ev:
            ctx = e["input"]
            assert "oracle" not in ctx and "expected" not in ctx and "case_id" not in ctx
            assert "token_hash" not in ctx["grant"]
            assert ctx["task"]["trace_id"] == b["job"]["input"]["task"]["trace_id"]
        if not case.startswith("L"):
            ctx = ev[-1]["input"]
            raw = b["job"]["input"]["call"]["arguments"]
            assert ctx["call"]["arguments"] == raw
            assert ctx["argument_types"] == ({k: type(v).__name__ for k, v in raw.items()} if isinstance(raw, dict) else None)
        else:
            assert ev[0]["result"]["allow"] is False
            assert ev[0]["result"]["reason"] == ao["response"]["body"]["error"]["data"]["reason"]
    adapter = OpaAdapter()
    context = rows["T01", "opa"]["observed"]["opa_evaluations"][-1]["input"]
    context_checks = []
    for name, mutate, expected in [
        ("task_agent_mismatch", lambda c: c["task"].update(agent_id="other"), "TOOL_GRANT_TASK_MISMATCH"),
        ("task_completed", lambda c: c["task"].update(status="completed"), "TOOL_GRANT_TASK_TERMINAL"),
        ("authentication_false", lambda c: c.update(authenticated=False), "TOOL_GRANT_INVALID"),
        ("missing_time", lambda c: c.pop("now"), "OPA_CONTEXT_INVALID"),
    ]:
        ctx = copy.deepcopy(context)
        mutate(ctx)
        actual = adapter.evaluate(ctx)
        assert actual == {"allow": False, "reason": expected}, (name, actual)
        context_checks.append({"name": name, "observed": actual})
    faults = []
    simulations = [
        ("nonzero_exit", subprocess.CompletedProcess([], 2, "", "injected process failure")),
        ("undefined", subprocess.CompletedProcess([], 0, '{"result":[]}', "")),
        ("malformed", subprocess.CompletedProcess([], 0, '{"result":[{"expressions":[{"value":true}]}]}', "")),
        ("timeout", subprocess.TimeoutExpired("inert-fault-probe", 20)),
    ]
    for name, value in simulations:
        kwargs = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
        with patch("run_opa_context_pilot.subprocess.run", **kwargs):
            actual = adapter.evaluate(context)
        assert actual == {"allow": False, "reason": "OPA_EVAL_ERROR"}
        faults.append({"name": name, "result": actual, "error": adapter.evaluations[-1]["error"]})
    # One real integration fault: nonexistent local executable, no network.
    adapter.command = [str(ROOT / "tmp/rr022-baselines/no-such-opa-fixture.exe")]
    adapter.evaluations = []
    with patch("run_opa_context_pilot.OpaAdapter", return_value=adapter):
        integration_fault = asyncio.run(run_method(rows["T01", "opa"]["job"], "opa"))
    assert integration_fault["response"]["status"] == 403
    assert integration_fault["tool_receipts"] == [] and integration_fault["engine_errors"]
    # Explicitly disclose, do not mask, the current shared-host audit whitelist.
    assert integration_fault["call_audit_count"] == 0
    check = subprocess.run(["wsl.exe", "-e", wsl_path(OPA), "check", "--strict", wsl_path(POLICY)], capture_output=True, text=True)
    assert check.returncode == 0
    result = {"paired_checks_passed": True, "main_cases": 9, "diagnostic_controls": 9,
              "result_sha256": file_hash(args.result), "checker_sha256": file_hash(Path(__file__)),
              "rego_check": {"exit_code": check.returncode, "stdout": check.stdout, "stderr": check.stderr},
              "additional_context_checks": context_checks, "adapter_fault_probes": faults,
              "integration_engine_failure": integration_fault,
              "remaining_gap": "Engine failure denies and creates no receipt, but the shared native rejection-audit whitelist does not record OPA_EVAL_ERROR. Engine errors must be reported separately, not counted as successful policy denials."}
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({"paired_checks": "pass", "context_checks": len(context_checks), "fault_probes": len(faults),
                      "integration_failure_receipts": 0, "integration_failure_call_audits": 0, "output": str(args.output)}))


if __name__ == "__main__":
    main()
