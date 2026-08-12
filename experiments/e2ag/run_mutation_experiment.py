"""Fixed-seed systematic mutations for E2AG contract and policy evaluation."""

from __future__ import annotations

import argparse
import copy
import json
import random
import sys
from collections import Counter, defaultdict
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.e2ag import evaluate_contract, evaluate_policy  # noqa: E402

HERE = Path(__file__).resolve().parent
SEED = 20260812


def load_benign() -> list[dict]:
    with (HERE / "attack_cases.jsonl").open(encoding="utf-8") as handle:
        return [case for case in map(json.loads, handle) if case["class"] == "benign"]


def violating_contract_valid_source(case: dict) -> str | None:
    event_type = str(case["event"]["type"])
    candidates = []
    if event_type.startswith("git."):
        candidates = ["github/mutated/repo", "gitlab/mutated/repo", "gitea/mutated/repo"]
    elif event_type.startswith("email."):
        candidates = ["imap/mutated"]
    elif event_type.startswith("webhook."):
        candidates = ["webhook/mutated"]
    elif event_type == "manual.trigger":
        candidates = ["manual/mutated"]
    elif event_type == "cron.tick":
        candidates = ["cron/mutated"]
    allowed_patterns = [
        pattern
        for capabilities in case["targets"].values()
        for pattern in capabilities.get("governance", {}).get("allowed_event_sources", [])
    ]
    return next(
        (source for source in candidates if not any(fnmatchcase(source, pattern) for pattern in allowed_patterns)),
        None,
    )


def variants(base: dict, rng: random.Random, per_operator: int) -> list[dict]:
    operators = (
        "drop_required",
        "invalid_specversion",
        "source_type_cross",
        "unknown_type",
        "target_source_violation",
        "target_tool_violation",
        "sensitive_prod_action",
    )
    outputs: list[dict] = []
    for operator in operators:
        for index in range(per_operator):
            eligible = base
            if operator == "target_tool_violation":
                eligible = [
                    item for item in base
                    if all(
                        capabilities.get("governance", {}).get("allowed_tools")
                        for capabilities in item["targets"].values()
                    )
                ]
            elif operator == "target_source_violation":
                eligible = [item for item in base if violating_contract_valid_source(item)]
            case = copy.deepcopy(rng.choice(eligible))
            event = case["event"]
            expected = "deny"
            if operator == "drop_required":
                field = rng.choice(["specversion", "id", "source", "type", "data"])
                event.pop(field, None)
            elif operator == "invalid_specversion":
                event["specversion"] = rng.choice(["0.3", "2.0", "", None])
            elif operator == "source_type_cross":
                if str(event["type"]).startswith("git."):
                    event["source"] = "imap/mutated"
                else:
                    event["source"] = "github/mutated/repo"
            elif operator == "unknown_type":
                event["type"] = f"unknown.mutated.{index}"
            elif operator == "target_source_violation":
                event["source"] = violating_contract_valid_source(case)
            elif operator == "target_tool_violation":
                event["data"]["requested_tool"] = f"admin.mutated_{index}"
            elif operator == "sensitive_prod_action":
                sensitive_action = rng.choice([
                    "secret.read", "admin.delete", "credential.rotate",
                    "filesystem.delete", "network.production.write",
                ])
                event["data"]["requested_action"] = sensitive_action
                event["data"]["environment"] = "production"
                for capabilities in case["targets"].values():
                    governance = capabilities.setdefault("governance", {})
                    governance["allowed_actions"] = [sensitive_action]
                expected = "approval_required"
            outputs.append({
                "id": f"M-{operator}-{index:03d}",
                "operator": operator,
                "expected": expected,
                "event": event,
                "targets": case["targets"],
            })
    return outputs


def decide(case: dict, mode: str) -> str:
    if mode == "B0_no_governance":
        return "allow"
    contract = evaluate_contract(case["event"])
    if mode == "B1_contract_only":
        return contract.decision
    return evaluate_policy(case["event"], contract, case["targets"]).decision


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-operator", type=int, default=100)
    args = parser.parse_args()
    rng = random.Random(SEED)
    cases = variants(load_benign(), rng, args.per_operator)
    results = []
    for mode in ("B0_no_governance", "B1_contract_only", "B2_full_e2ag"):
        decisions = [{**case, "actual": decide(case, mode)} for case in cases]
        by_operator = defaultdict(Counter)
        for row in decisions:
            by_operator[row["operator"]]["cases"] += 1
            by_operator[row["operator"]]["correct"] += row["actual"] == row["expected"]
            by_operator[row["operator"]]["non_allow"] += row["actual"] != "allow"
        results.append({
            "mode": mode,
            "cases": len(decisions),
            "exact_accuracy": sum(row["actual"] == row["expected"] for row in decisions) / len(decisions),
            "non_allow_rate": sum(row["actual"] != "allow" for row in decisions) / len(decisions),
            "by_operator": {
                name: {
                    "cases": count["cases"],
                    "exact_accuracy": count["correct"] / count["cases"],
                    "non_allow_rate": count["non_allow"] / count["cases"],
                }
                for name, count in sorted(by_operator.items())
            },
        })
    output = {
        "seed": SEED,
        "base_benign_cases": 8,
        "per_operator": args.per_operator,
        "synthetic_cases": len(cases),
        "operators": sorted({case["operator"] for case in cases}),
        "limitations": "Synthetic fixed-seed mutations of hand-authored benign cases; not a real-world prevalence estimate.",
        "results": results,
    }
    destination = HERE / "results" / "mutation_summary.json"
    destination.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
