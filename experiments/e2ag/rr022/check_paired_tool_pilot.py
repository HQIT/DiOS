"""Harness checks, including policy reversal; never counted as workload samples."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from run_paired_tool_pilot import HERE, ROOT, digest, file_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Choose a new evidence filename")
    report = json.loads(args.result.read_text(encoding="utf-8"))
    assert report["passed"] == report["total"] == 4
    for case in ("T01", "T02"):
        pair = [r for r in report["rows"] if r["case"] == case]
        assert len(pair) == 2
        assert pair[0]["observed"]["input_sha256"] == pair[1]["observed"]["input_sha256"]
        assert pair[0]["observed"]["tool_receipts"] == pair[1]["observed"]["tool_receipts"]
    specs = [r["observed"] for r in report["rows"] if r["method"] == "agentspec"]
    assert specs[0]["native_rule_texts"] == specs[1]["native_rule_texts"]
    for row in report["rows"]:
        if row["method"] == "e2ag":
            assert row["observed"]["audit_verification"] is True
            assert row["observed"]["grant_call_count"] == 1  # attempt, NOT executed-tool count
    methods = {"e2ag": ROOT / "backend/.venv/Scripts/python.exe",
               "agentspec": ROOT / "tmp/rr022-baselines/.venv-agentspec/Scripts/python.exe"}
    checks = []
    for row in report["rows"]:
        policy = copy.deepcopy(report["policy"])
        policy["allowed_tools"] = ["report.preview"]
        job = {"policy": policy, "input": row["input"]}
        proc = subprocess.run([str(methods[row["method"]]), str(HERE / "run_paired_tool_pilot.py"),
                               "--worker", row["method"]], input=json.dumps(job), text=True,
                              encoding="utf-8", capture_output=True, cwd=ROOT, timeout=90)
        observed = json.loads(proc.stdout)
        expected_count = 0 if row["case"] == "T01" else 1
        passed = (proc.returncode == 0 and observed.get("error") is None
                  and observed["decision"] == ("allow" if expected_count else "deny")
                  and len(observed["tool_receipts"]) == expected_count
                  and observed["input_sha256"] == digest(job))
        checks.append({"source_case": row["case"], "method": row["method"], "policy": policy,
                       "observed": observed, "stderr": proc.stderr, "pass": passed})
        print(json.dumps({"case": row["case"], "method": row["method"], "policy_reversal_pass": passed}), flush=True)
    result = {"kind": "harness validation, not independent workload", "original_result_sha256": file_hash(args.result),
              "checker_sha256": file_hash(Path(__file__)), "paired_harness_sha256": file_hash(HERE / "run_paired_tool_pilot.py"),
              "structural_checks": "passed: identical input/receipts, same static rules, valid native audit chains",
              "policy_reversal_checks": checks, "passed": sum(c["pass"] for c in checks), "total": len(checks)}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    assert all(c["pass"] for c in checks)


if __name__ == "__main__":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    main()
