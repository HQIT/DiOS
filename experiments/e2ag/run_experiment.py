"""Deterministic E2AG ablation and microbenchmark runner."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.e2ag import evaluate_contract, evaluate_policy  # noqa: E402


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * q))
    return ordered[index]


def decide(case: dict, mode: str) -> str:
    if mode == "B0_no_governance":
        return "allow"
    contract = evaluate_contract(case["event"])
    if mode == "B1_contract_only":
        return contract.decision
    return evaluate_policy(case["event"], contract, case.get("targets") or {}).decision


def run(cases: list[dict], repeats: int) -> tuple[list[dict], list[dict]]:
    modes = ("B0_no_governance", "B1_contract_only", "B2_full_e2ag")
    case_rows: list[dict] = []
    summaries: list[dict] = []
    for mode in modes:
        latencies: list[float] = []
        decisions: dict[str, str] = {}
        for case in cases:
            for _ in range(repeats):
                start = perf_counter_ns()
                decision = decide(case, mode)
                latencies.append((perf_counter_ns() - start) / 1_000)
            decisions[case["id"]] = decision
            case_rows.append({
                "mode": mode,
                "case_id": case["id"],
                "class": case["class"],
                "expected": case["expected"],
                "decision": decision,
                "correct": int(decision == case["expected"]),
            })
        attacks = [c for c in cases if c["class"] == "attack"]
        benign = [c for c in cases if c["class"] == "benign"]
        attack_success = sum(decisions[c["id"]] == "allow" for c in attacks)
        benign_allowed = sum(decisions[c["id"]] == "allow" for c in benign)
        exact = sum(decisions[c["id"]] == c["expected"] for c in cases)
        summaries.append({
            "mode": mode,
            "cases": len(cases),
            "attacks": len(attacks),
            "benign": len(benign),
            "attack_success_rate": attack_success / len(attacks),
            "attack_prevention_rate": 1 - attack_success / len(attacks),
            "benign_pass_rate": benign_allowed / len(benign),
            "exact_decision_accuracy": exact / len(cases),
            "latency_p50_us": round(percentile(latencies, 0.50), 2),
            "latency_p95_us": round(percentile(latencies, 0.95), 2),
            "latency_p99_us": round(percentile(latencies, 0.99), 2),
            "measurements": len(latencies),
        })
    return case_rows, summaries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("results"))
    args = parser.parse_args()
    cases_path = Path(__file__).with_name("attack_cases.jsonl")
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    case_rows, summaries = run(cases, args.repeats)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    with (args.output / "case_results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=case_rows[0].keys())
        writer.writeheader()
        writer.writerows(case_rows)
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
