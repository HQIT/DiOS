"""Evaluate the frozen MCP tool-call slice with the official OPA engine.

This is a functional external-engine baseline, not a latency benchmark.  The
Rego policy receives the same task-level tool allowlist as E2AG.  Cases outside
``tools/call`` are excluded because they test the MCP protocol boundary rather
than tool authorization and are not inputs to this baseline policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent
POLICY = HERE / "baselines" / "opa_tool_allowlist.rego"
POLICY_DATA = HERE / "baselines" / "opa_tool_allowlist_data.json"
CASES = HERE / "tool_call_cases.jsonl"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in CASES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run_opa(opa_bin: Path, case: dict) -> bool:
    request = {"method": case["method"], "tool": case["tool"]}
    completed = subprocess.run(
        [
            str(opa_bin),
            "eval",
            "--format=json",
            "--data",
            str(POLICY),
            "--data",
            str(POLICY_DATA),
            "--stdin-input",
            "data.e2ag.tool_boundary.allow",
        ],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    return bool(payload["result"][0]["expressions"][0]["value"])


def opa_version(opa_bin: Path) -> dict:
    completed = subprocess.run(
        [str(opa_bin), "version"],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    values = {}
    for line in completed.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().lower().replace(" ", "_")] = value.strip()
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opa-bin", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=HERE / "results" / "opa_tool_baseline_summary.json",
    )
    parser.add_argument("--expected-opa-sha256")
    args = parser.parse_args()

    opa_bin = args.opa_bin.resolve()
    if not opa_bin.is_file():
        raise FileNotFoundError(opa_bin)
    binary_sha256 = sha256(opa_bin)
    if args.expected_opa_sha256 and binary_sha256 != args.expected_opa_sha256.lower():
        raise ValueError("OPA binary SHA-256 does not match the expected release digest")

    cases = load_cases()
    evaluated = [case for case in cases if case["method"] == "tools/call"]
    excluded = [case for case in cases if case["method"] != "tools/call"]
    if len(evaluated) != 8 or len(excluded) != 2:
        raise ValueError("expected an 8-case tools/call slice and 2 protocol-boundary cases")

    rows = []
    for case in evaluated:
        actual = "allow" if run_opa(opa_bin, case) else "deny"
        rows.append({
            "id": case["id"],
            "label": case["label"],
            "method": case["method"],
            "tool": case["tool"],
            "expected": case["expected"],
            "actual": actual,
            "correct": actual == case["expected"],
        })

    benign = [row for row in rows if row["label"] == "benign"]
    violations = [row for row in rows if row["label"] == "attack"]
    result = {
        "protocol": "external-opa-tool-boundary-v1",
        "baseline": {
            "name": "OPA-Tool",
            "engine": "Open Policy Agent",
            "upstream": "https://github.com/open-policy-agent/opa",
            "license": "Apache-2.0",
            "version": opa_version(opa_bin),
            "binary_sha256": binary_sha256,
            "policy_sha256": sha256(POLICY),
            "policy_data_sha256": sha256(POLICY_DATA),
        },
        "corpus": {
            "path": "experiments/e2ag/tool_call_cases.jsonl",
            "sha256": sha256(CASES),
            "total_cases": len(cases),
            "evaluated_tools_call_cases": len(evaluated),
            "excluded_protocol_boundary_cases": [case["id"] for case in excluded],
            "exclusion_reason": (
                "These cases evaluate non-tools/call MCP method mediation, which is "
                "outside the OPA-Tool policy input object."
            ),
        },
        "policy_input": {
            "allowed_tools": json.loads(POLICY_DATA.read_text(encoding="utf-8"))["allowed_tools"],
            "adaptation": (
                "A generic default-deny Rego allowlist using the same tool patterns; "
                "no E2AG implementation code is called."
            ),
        },
        "metrics": {
            "cases": len(rows),
            "benign_cases": len(benign),
            "policy_violation_cases": len(violations),
            "benign_pass_rate": sum(row["actual"] == "allow" for row in benign) / len(benign),
            "policy_violation_prevention_rate": (
                sum(row["actual"] == "deny" for row in violations) / len(violations)
            ),
            "decision_accuracy": sum(row["correct"] for row in rows) / len(rows),
        },
        "case_results": rows,
        "interpretation": (
            "OPA-Tool checks the same fixed tool-call allowlist with an independent "
            "policy engine. It does not implement event-source binding, task admission, "
            "task-scoped grant lifecycle, or cross-layer evidence continuity. No latency "
            "comparison is reported."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
