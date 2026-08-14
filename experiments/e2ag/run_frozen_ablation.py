"""Run the frozen Contract x Policy 2x2 E2AG evaluation.

The corpus is intentionally separate from the original 22-case development set.
All events are structurally valid so that C0P1 means "policy without source-type
binding", rather than "policy after a parser failure".
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from app.services.e2ag import Decision, evaluate_contract, evaluate_policy  # noqa: E402

MODES = ("C0P0", "C1P0", "C0P1", "C1P1")
SEED = 20260814


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def expected(case: dict, mode: str) -> str:
    layer = case["layer"]
    if layer == "benign":
        return "allow"
    if layer == "contract":
        return "deny" if mode in {"C1P0", "C1P1"} else "allow"
    if layer == "policy":
        return "deny" if mode in {"C0P1", "C1P1"} else "allow"
    if layer == "approval":
        return "approval_required" if mode in {"C0P1", "C1P1"} else "allow"
    raise ValueError(f"unknown layer: {layer}")


def decide(case: dict, mode: str) -> str:
    if mode == "C0P0":
        return "allow"
    if mode == "C1P0":
        return evaluate_contract(case["event"]).decision
    if mode == "C0P1":
        bypass = Decision(
            stage="contract",
            decision="allow",
            reason_codes=("EXPERIMENT_CONTRACT_BINDING_DISABLED",),
            contract_type="experiment-bypass",
        )
        return evaluate_policy(case["event"], bypass, case.get("targets") or {}).decision
    contract = evaluate_contract(case["event"])
    return evaluate_policy(case["event"], contract, case.get("targets") or {}).decision


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float]:
    if total == 0:
        return [0.0, 0.0]
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, centre - margin), 6), round(min(1.0, centre + margin), 6)]


def evaluate(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    summaries: list[dict] = []
    for mode in MODES:
        for case in cases:
            actual = decide(case, mode)
            wanted = expected(case, mode)
            rows.append({
                "mode": mode,
                "case_id": case["id"],
                "class": case["class"],
                "layer": case["layer"],
                "scenario": case["scenario"],
                "expected": wanted,
                "actual": actual,
                "correct": int(actual == wanted),
            })
        mode_rows = [row for row in rows if row["mode"] == mode]
        attacks = [row for row in mode_rows if row["class"] == "attack"]
        benign = [row for row in mode_rows if row["class"] == "benign"]
        prevented = sum(row["actual"] != "allow" for row in attacks)
        passed = sum(row["actual"] == "allow" for row in benign)
        by_layer: dict[str, dict] = {}
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in mode_rows:
            grouped[row["layer"]].append(row)
        for layer, layer_rows in sorted(grouped.items()):
            by_layer[layer] = {
                "cases": len(layer_rows),
                "non_allow": sum(row["actual"] != "allow" for row in layer_rows),
                "exact_correct": sum(row["correct"] for row in layer_rows),
            }
        summaries.append({
            "mode": mode,
            "cases": len(mode_rows),
            "attacks": len(attacks),
            "benign": len(benign),
            "attacks_prevented": prevented,
            "attack_prevention_rate": prevented / len(attacks),
            "attack_prevention_wilson95": wilson(prevented, len(attacks)),
            "benign_passed": passed,
            "benign_pass_rate": passed / len(benign),
            "benign_pass_wilson95": wilson(passed, len(benign)),
            "exact_correct": sum(row["correct"] for row in mode_rows),
            "exact_accuracy": sum(row["correct"] for row in mode_rows) / len(mode_rows),
            "by_layer": by_layer,
        })
    return rows, summaries


def paired_overhead(cases: list[dict], batches: int, repeats_per_batch: int) -> dict:
    rng = random.Random(SEED)
    # Warm the registry and Python paths before collecting paired batches.
    for mode in MODES:
        for case in cases:
            decide(case, mode)
    samples: dict[str, list[float]] = {mode: [] for mode in MODES}
    orders: list[list[str]] = []
    for _ in range(batches):
        order = list(MODES)
        rng.shuffle(order)
        orders.append(order)
        for mode in order:
            started = perf_counter_ns()
            for _repeat in range(repeats_per_batch):
                for case in cases:
                    decide(case, mode)
            elapsed = perf_counter_ns() - started
            samples[mode].append(elapsed / (repeats_per_batch * len(cases)) / 1_000)
    summary = {}
    baseline = statistics.median(samples["C0P0"])
    for mode in MODES:
        ordered = sorted(samples[mode])
        median = statistics.median(ordered)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
        summary[mode] = {
            "batches": batches,
            "decisions_per_batch": repeats_per_batch * len(cases),
            "median_batch_mean_us_per_decision": round(median, 3),
            "p95_batch_mean_us_per_decision": round(p95, 3),
            "relative_to_C0P0": round(median / baseline, 3) if baseline else None,
        }
    return {
        "interpretation": (
            "Same-host paired engineering observation only; absolute latency and "
            "relative factors do not establish production end-to-end performance."
        ),
        "seed": SEED,
        "randomized_mode_orders": orders,
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=HERE / "frozen_cases.jsonl")
    parser.add_argument("--output", type=Path, default=HERE / "results")
    parser.add_argument("--latency-batches", type=int, default=30)
    parser.add_argument("--latency-repeats", type=int, default=20)
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if len(cases) != 60:
        raise ValueError(f"frozen corpus must contain 60 cases, got {len(cases)}")
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("frozen corpus contains duplicate case ids")
    if sum(case["class"] == "benign" for case in cases) != 30:
        raise ValueError("frozen corpus must contain 30 benign cases")
    if sum(case["class"] == "attack" for case in cases) != 30:
        raise ValueError("frozen corpus must contain 30 attack cases")

    rows, summaries = evaluate(cases)
    result = {
        "protocol": "frozen-contract-policy-2x2-v1",
        "corpus_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "review_status": "pending_independent_collaborator_review",
        "modes": {
            "C0P0": {"contract_binding": False, "target_policy": False},
            "C1P0": {"contract_binding": True, "target_policy": False},
            "C0P1": {"contract_binding": False, "target_policy": True},
            "C1P1": {"contract_binding": True, "target_policy": True},
        },
        "results": summaries,
        "paired_overhead": paired_overhead(cases, args.latency_batches, args.latency_repeats),
        "limitations": [
            "Threat-driven author-frozen corpus; not a prevalence sample.",
            "Independent collaborator label review is pending.",
            "All cases are structurally valid to isolate source-type binding from target policy.",
        ],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "frozen_ablation_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (args.output / "frozen_ablation_cases.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
