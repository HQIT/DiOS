"""Deterministic E2AG runtime tool-gateway ablation and microbenchmark."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.e2ag_tool_gateway import authorize_mcp_request  # noqa: E402

HERE = Path(__file__).resolve().parent
ALLOWED_TOOLS = ["ledger.record_*", "repo.read", "mail.send"]


def percentile(values: list[int], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(q * len(ordered)))
    return ordered[index] / 1000.0


def load_cases() -> list[dict]:
    with (HERE / "tool_call_cases.jsonl").open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def decide(mode: str, case: dict) -> str:
    if mode == "R0_dispatch_only":
        return "allow"
    allowed, _ = authorize_mcp_request(
        case["method"], {"name": case["tool"]}, ALLOWED_TOOLS,
    )
    return "allow" if allowed else "deny"


def evaluate(mode: str, cases: list[dict], repeats: int) -> dict:
    rows = [{**case, "actual": decide(mode, case)} for case in cases]
    attacks = [row for row in rows if row["label"] == "attack"]
    benign = [row for row in rows if row["label"] == "benign"]
    timings: list[int] = []
    for _ in range(repeats):
        for case in cases:
            started = time.perf_counter_ns()
            decide(mode, case)
            timings.append(time.perf_counter_ns() - started)
    return {
        "mode": mode,
        "cases": len(rows),
        "attack_cases": len(attacks),
        "benign_cases": len(benign),
        "attack_prevention_rate": sum(row["actual"] == "deny" for row in attacks) / len(attacks),
        "benign_pass_rate": sum(row["actual"] == "allow" for row in benign) / len(benign),
        "decision_accuracy": sum(row["actual"] == row["expected"] for row in rows) / len(rows),
        "latency_us": {
            "p50": round(percentile(timings, 0.50), 3),
            "p95": round(percentile(timings, 0.95), 3),
            "p99": round(percentile(timings, 0.99), 3),
        },
        "case_results": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=10000)
    args = parser.parse_args()
    cases = load_cases()
    results = [
        evaluate("R0_dispatch_only", cases, args.repeats),
        evaluate("R1_runtime_e2ag", cases, args.repeats),
    ]
    output = {
        "allowed_tools": ALLOWED_TOOLS,
        "repeats_per_case": args.repeats,
        "interpretation": (
            "Pure in-process authorization cost; proxy forwarding, database, "
            "network, model, and container latency are excluded."
        ),
        "results": results,
    }
    destination = HERE / "results" / "tool_gateway_summary.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    for result in results:
        latency = result["latency_us"]
        print(
            f'{result["mode"]}: prevention={result["attack_prevention_rate"]:.2%}, '
            f'benign_pass={result["benign_pass_rate"]:.2%}, '
            f'p50/p95/p99={latency["p50"]}/{latency["p95"]}/{latency["p99"]} us'
        )
    print(destination)


if __name__ == "__main__":
    main()
