"""Successor to the preserved OPA call pilot: typed engine failures + audit."""
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
import run_opa_context_pilot as previous
from run_paired_tool_pilot import ROOT, HERE, file_hash, digest

OriginalAdapter = previous.OpaAdapter


class AuditedOpaAdapter(OriginalAdapter):
    def evaluate(self, context):
        from app.services.e2ag_policy_errors import PolicyEvaluationError
        result = super().evaluate(context)
        if self.evaluations[-1]["error"]:
            raise PolicyEvaluationError("OPA adapter failed to obtain a decision")
        return result


class UnavailableAdapter(AuditedOpaAdapter):
    def __init__(self, phase):
        super().__init__()
        self.fail_phase = phase

    def evaluate(self, context):
        if context["phase"] == self.fail_phase:
            self.command = [str(ROOT / "tmp/rr022-baselines/no-such-opa-fixture.exe")]
        return super().evaluate(context)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assert not args.output.exists()
    spec_path = HERE / "common-cases-v0.1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    before_path = HERE / "results/opa-context-pilot-20260930-v1.json"
    before = json.loads(before_path.read_text(encoding="utf-8"))
    before_rows = {(r["case"], r["method"]): r for r in before["rows"]}
    rows = []
    for name, job, count in previous.jobs(spec):
        for method in ("e2ag", "opa"):
            with patch.object(previous, "OpaAdapter", AuditedOpaAdapter):
                observed = asyncio.run(previous.run_method(job, method))
            old = before_rows[name, method]
            assert job == old["job"] and observed["input_sha256"] == digest(job)
            assert observed["decision"] == old["observed"]["decision"]
            assert observed["tool_receipts"] == old["observed"]["tool_receipts"]
            assert len(observed["tool_receipts"]) == count
            assert observed["grant_status"] == old["observed"]["grant_status"]
            assert observed["grant_call_count"] == observed["call_audit_count"] == 1
            assert observed["audit_verification"] and not observed["engine_errors"]
            rows.append({"case": name, "method": method, "control": name.startswith("C_"),
                         "job": job, "observed": observed, "pass": True})
            print(json.dumps({"case": name, "method": method, "unchanged": True}), flush=True)
    normal = {"policy": copy.deepcopy(spec["policy"]), "input": copy.deepcopy(spec["base_input"])}
    normal["input"]["approval"].pop("answer", None)
    faults = []
    for phase in ("grant", "call"):
        with patch.object(previous, "OpaAdapter", lambda: UnavailableAdapter(phase)):
            observed = asyncio.run(previous.run_method(normal, "opa"))
        assert observed["response"]["status"] == 503
        assert observed["response"]["body"]["error"]["data"]["reason"] == "POLICY_ENGINE_ERROR"
        assert not observed["tool_receipts"] and observed["call_audit_count"] == 1
        assert observed["grant_call_count"] == 1 and observed["grant_status"] == "active"
        assert observed["audit_verification"] and observed["engine_errors"]
        entry = observed["audit_added"][-1]
        assert entry["stage"] == "tool_call" and entry["evidence"]["reason"] == "POLICY_ENGINE_ERROR"
        faults.append({"phase": phase, "observed": observed, "classification": "engine_failure_not_policy_denial"})
    tests = subprocess.run([str(ROOT / "backend/.venv/Scripts/python.exe"), "-m", "unittest", "discover", "-s", "tests", "-p", "test_e2ag*.py"],
                           cwd=ROOT / "backend", capture_output=True, text=True, encoding="utf-8", timeout=180)
    source = [Path(__file__), Path(previous.__file__), previous.POLICY,
              ROOT / "backend/app/api/internal/e2ag_mcp.py", ROOT / "backend/app/services/e2ag_tool_gateway.py",
              ROOT / "backend/app/services/e2ag_policy_errors.py", ROOT / "backend/tests/test_e2ag_policy_errors.py"]
    result = {"protocol": "rr022-opa-call-audited-v2", "rows": rows, "normal_decisions_unchanged": True,
              "before_sha256": file_hash(before_path), "cases_sha256": file_hash(spec_path),
              "source_sha256": {str(p.relative_to(ROOT)): file_hash(p) for p in source},
              "faults": faults, "backend_regression": {"exit_code": tests.returncode, "stdout": tests.stdout, "stderr": tests.stderr},
              "scope": "POST policy failures only. Prior driver/policy unchanged; successor uses sequential fresh databases in one process. No latency measurement."}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"normal_rows": len(rows), "faults_with_audit": len(faults), "regression_exit": tests.returncode}))
    if tests.returncode:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
