"""Check paired evidence, preserved pre-extension gaps, and backend regression."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, HERE, digest, file_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    before, after = [json.loads(p.read_text(encoding="utf-8")) for p in (args.before, args.after)]
    assert before["harness_sha256"] == after["harness_sha256"] == file_hash(HERE / "run_parameter_pilot.py")
    assert before["cases_sha256"] == after["cases_sha256"]
    assert before["helper_sha256"] == after["helper_sha256"]
    for key in ("jar_sha256", "original_grammar_sha256"):
        assert before["parser_generation"][key] == after["parser_generation"][key]
    old = {(r["case"], r["method"]): r for r in before["rows"]}
    new = {(r["case"], r["method"]): r for r in after["rows"]}
    assert len(old) == len(new) == 30 and old.keys() == new.keys()
    for key, row in new.items():
        prev = old[key]
        assert row["job"] == prev["job"] and row["expected_receipts"] == prev["expected_receipts"]
        obs, pre = row["observed"], prev["observed"]
        assert not obs["error"] and not pre["error"] and row["pass"]
        assert obs["input_sha256"] == digest(row["job"])
        assert obs["dependencies"] == pre["dependencies"]
        assert obs["model_calls"] == 0
        assert len(obs["tool_receipts"]) == row["expected_receipts"]
        for item in obs["tool_receipts"]:
            assert item == {"tool": row["job"]["input"]["call"]["tool"],
                            "arguments": row["job"]["input"]["call"]["arguments"], "sequence": 1}
        if row["method"] == "agentspec":
            assert prev["pass"]
            assert obs["source_sha256"] == pre["source_sha256"]
            assert obs["native_rule_texts"] == pre["native_rule_texts"]
            assert obs["predicate_evaluations"] == pre["predicate_evaluations"]
            assert obs["tool_receipts"] == pre["tool_receipts"]
            if row["case"] != "T02":
                assert len(obs["predicate_evaluations"]) == 1
                assert obs["predicate_evaluations"][0]["actual_arguments"] == row["job"]["input"]["call"]["arguments"]
        else:
            assert pre["stored_constraints"] is None
            assert obs["stored_constraints"] == obs["configured_constraints"]
            assert obs["audit_verification"] is True
            assert obs["audit_chain"][-1]["stage"] == "tool_call"
            assert obs["audit_chain"][-1]["outcome"] == obs["decision"]
            assert len(obs["audit_chain"][-1]["evidence"]["argument_constraints_sha256"]) == 64
            assert obs["response"]["status"] == (200 if row["expected_receipts"] else 403)
    for case_id in {r["case"] for r in after["rows"]}:
        a, b = new[case_id, "e2ag"], new[case_id, "agentspec"]
        assert a["job"] == b["job"]
        assert a["observed"]["decision"] == b["observed"]["decision"]
        assert a["observed"]["tool_receipts"] == b["observed"]["tool_receipts"]
    for case_id in ("T03", "T04", "T05"):
        assert not old[case_id, "e2ag"]["pass"]
        assert len(old[case_id, "e2ag"]["observed"]["tool_receipts"]) == 1
    run = subprocess.run([str(ROOT / "backend/.venv/Scripts/python.exe"), "-m", "unittest", "discover", "-s", "tests", "-p", "test_e2ag*.py"],
                         cwd=ROOT / "backend", capture_output=True, text=True, encoding="utf-8", timeout=180)
    report = {"evidence_checks_passed": True, "before_sha256": file_hash(args.before), "after_sha256": file_hash(args.after),
              "shared_main_cases": 6, "separate_diagnostic_controls": 9,
              "same_harness_inputs_policy_dependencies": True, "agentspec_unchanged_results": True,
              "backend_regression": {"exit_code": run.returncode, "stdout": run.stdout, "stderr": run.stderr},
              "source_sha256": {str(p.relative_to(ROOT)): file_hash(p) for p in [
                  ROOT / 'backend/app/services/e2ag_arguments.py', ROOT / 'backend/app/services/e2ag_tool_gateway.py',
                  ROOT / 'backend/app/api/internal/e2ag_mcp.py', ROOT / 'backend/app/models/tables.py', ROOT / 'backend/app/db/database.py',
                  ROOT / 'backend/tests/test_e2ag_argument_constraints.py']},
              "checker_sha256": file_hash(Path(__file__))}
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({"paired_evidence": "pass", "regression_exit": run.returncode, "output": str(args.output)}))
    if run.returncode:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
