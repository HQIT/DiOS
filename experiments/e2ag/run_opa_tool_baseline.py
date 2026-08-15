"""Evaluate the frozen MCP tool-call slice with the official OPA engine.

This is a functional external-engine baseline, not a latency benchmark.  The
Rego policy receives the same task-level tool allowlist as E2AG.  The primary
comparison uses the call-ready subset of the frozen 60-case matrix: a case must
have passed contract construction, name a requested tool, and expose a target
tool allowlist.  The small MCP method/tool suite remains a supplementary
interface check.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent
POLICY = HERE / "baselines" / "opa_tool_allowlist.rego"
POLICY_DATA = HERE / "baselines" / "opa_tool_allowlist_data.json"
CASES = HERE / "tool_call_cases.jsonl"
FROZEN_CASES = HERE / "frozen_cases.jsonl"
FROZEN_ABLATION_ROWS = HERE / "results" / "frozen_ablation_cases.csv"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in CASES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run_opa(opa_bin: Path, *, method: str, tool: str, allowed_tools: list[str]) -> bool:
    request = {
        "method": method,
        "tool": tool,
        "allowed_tools": allowed_tools,
    }
    completed = subprocess.run(
        [
            str(opa_bin),
            "eval",
            "--format=json",
            "--data",
            str(POLICY),
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


def load_frozen_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in FROZEN_CASES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def select_call_ready_cases(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    selected = []
    excluded = []
    for case in cases:
        targets = case.get("targets") or {}
        target = next(iter(targets.values()), {}) if targets else {}
        governance = target.get("governance") or {}
        tool = str((case.get("event", {}).get("data") or {}).get("requested_tool") or "")
        allowed_tools = governance.get("allowed_tools")
        if targets and tool and isinstance(allowed_tools, list):
            selected.append({
                **case,
                "requested_tool": tool,
                "allowed_tools": [str(pattern) for pattern in allowed_tools],
            })
        else:
            excluded.append(case)
    return selected, excluded


def evaluate_call_ready_slice(opa_bin: Path) -> dict:
    frozen = load_frozen_cases()
    selected, excluded = select_call_ready_cases(frozen)
    benign = [case for case in selected if case["class"] == "benign"]
    violations = [case for case in selected if case["class"] == "attack"]
    if len(frozen) != 60 or len(selected) != 49 or len(benign) != 30 or len(violations) != 19:
        raise ValueError("expected frozen=60, call-ready=49, benign=30, violations=19")

    rows = []
    for case in selected:
        allow = run_opa(
            opa_bin,
            method="tools/call",
            tool=case["requested_tool"],
            allowed_tools=case["allowed_tools"],
        )
        actual = "allow" if allow else "deny"
        expected = "allow" if case["class"] == "benign" else "non_allow"
        rows.append({
            "id": case["id"],
            "class": case["class"],
            "layer": case["layer"],
            "allowed_pattern_count": len(case["allowed_tools"]),
            "expected": expected,
            "actual": actual,
            "correct": actual == expected or (expected == "non_allow" and actual == "deny"),
        })

    benign_rows = [row for row in rows if row["class"] == "benign"]
    violation_rows = [row for row in rows if row["class"] == "attack"]
    prevented = sum(row["actual"] == "deny" for row in violation_rows)
    passed = sum(row["actual"] == "allow" for row in benign_rows)
    selected_by_id = {case["id"]: case for case in selected}
    with FROZEN_ABLATION_ROWS.open(encoding="utf-8", newline="") as handle:
        ablation_rows = list(csv.DictReader(handle))

    comparators = {}
    for mode, name in (("C0P0", "NoGuard"), ("C1P1", "E2AG")):
        mode_rows = [
            row for row in ablation_rows
            if row["mode"] == mode and row["case_id"] in selected_by_id
        ]
        if len(mode_rows) != len(selected):
            raise ValueError(f"expected {len(selected)} frozen rows for {mode}, got {len(mode_rows)}")
        benign_mode = [row for row in mode_rows if selected_by_id[row["case_id"]]["class"] == "benign"]
        violation_mode = [row for row in mode_rows if selected_by_id[row["case_id"]]["class"] == "attack"]
        benign_mode_passed = sum(row["actual"] == "allow" for row in benign_mode)
        violation_mode_non_allow = sum(row["actual"] != "allow" for row in violation_mode)
        comparators[name] = {
            "source_mode": mode,
            "source_sha256": sha256(FROZEN_ABLATION_ROWS),
            "benign_passed": benign_mode_passed,
            "benign_cases": len(benign_mode),
            "violations_non_allow": violation_mode_non_allow,
            "violation_cases": len(violation_mode),
            "exact_aligned": benign_mode_passed + violation_mode_non_allow,
            "selected_cases": len(mode_rows),
        }
    return {
        "selection_rule": (
            "A frozen case is call-ready when it has a non-empty target, an explicit "
            "requested_tool, and a target governance allowed_tools list."
        ),
        "frozen_corpus_sha256": sha256(FROZEN_CASES),
        "frozen_cases": len(frozen),
        "selected_cases": len(selected),
        "excluded_cases": len(excluded),
        "excluded_case_ids": [case["id"] for case in excluded],
        "excluded_reason": (
            "The excluded cases terminate before a comparable tool-authorization "
            "object exists or omit the requested tool; counting them as OPA denials "
            "would overstate tool-boundary coverage."
        ),
        "metrics": {
            "benign_cases": len(benign_rows),
            "cross_layer_violation_cases": len(violation_rows),
            "benign_passed": passed,
            "benign_pass_rate": passed / len(benign_rows),
            "violations_non_allow": prevented,
            "violation_non_allow_rate": prevented / len(violation_rows),
            "exact_aligned": passed + prevented,
            "exact_alignment_rate": (passed + prevented) / len(rows),
        },
        "by_layer": {
            layer: {
                "cases": len(layer_rows),
                "non_allow": sum(row["actual"] == "deny" for row in layer_rows),
                "allow": sum(row["actual"] == "allow" for row in layer_rows),
            }
            for layer in sorted({row["layer"] for row in rows})
            for layer_rows in [[row for row in rows if row["layer"] == layer]]
        },
        "comparators": comparators,
        "case_results": rows,
    }


def evaluate_protocol_slice(opa_bin: Path) -> dict:
    cases = load_cases()
    allowed_tools = json.loads(POLICY_DATA.read_text(encoding="utf-8"))["allowed_tools"]
    evaluated = [case for case in cases if case["method"] == "tools/call"]
    excluded = [case for case in cases if case["method"] != "tools/call"]
    if len(evaluated) != 8 or len(excluded) != 2:
        raise ValueError("expected an 8-case tools/call slice and 2 protocol-boundary cases")

    rows = []
    for case in evaluated:
        actual = "allow" if run_opa(
            opa_bin,
            method=case["method"],
            tool=case["tool"],
            allowed_tools=allowed_tools,
        ) else "deny"
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
    return {
        "corpus_sha256": sha256(CASES),
        "total_cases": len(cases),
        "evaluated_tools_call_cases": len(evaluated),
        "excluded_protocol_boundary_cases": [case["id"] for case in excluded],
        "exclusion_reason": (
            "These cases evaluate non-tools/call MCP method mediation, which is "
            "outside the OPA-Tool policy input object."
        ),
        "allowed_tools": allowed_tools,
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
    }


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

    call_ready = evaluate_call_ready_slice(opa_bin)
    protocol = evaluate_protocol_slice(opa_bin)
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
            "protocol_config_sha256": sha256(POLICY_DATA),
        },
        "primary_call_ready_slice": call_ready,
        "supplementary_protocol_slice": protocol,
        "policy_input": {
            "adaptation": (
                "A generic default-deny Rego allowlist receives each case's task-level "
                "allowed_tools; no E2AG implementation code is called."
            ),
        },
        "interpretation": (
            "OPA-Tool independently reproduces task-level tool-name allowlist decisions. "
            "On the call-ready frozen slice it can observe only tool-set mismatches; it "
            "does not receive event-source, task-admission, approval, grant-lifecycle, or "
            "cross-layer evidence state. This isolates protection-object coverage rather "
            "than ranking the OPA engine against the E2AG architecture. No latency "
            "comparison is reported."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
