"""Evaluate tamper detection coverage of the E2AG audit chain."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.e2ag import append_audit_entry, verify_audit_chain  # noqa: E402


def base_chain() -> list[dict]:
    chain: list[dict] = []
    for stage, outcome in (
        ("contract", "allow"),
        ("policy", "allow"),
        ("a2a_task", "submitted"),
        ("dispatch", "dispatched"),
    ):
        chain = append_audit_entry(
            chain,
            trace_id="trace-experiment",
            stage=stage,
            outcome=outcome,
            evidence={"stage": stage},
        )
    return chain


def mutations(chain: list[dict]) -> list[tuple[str, list[dict], bool]]:
    out: list[tuple[str, list[dict], bool]] = []

    value = deepcopy(chain)
    value[1]["outcome"] = "deny"
    out.append(("content_change", value, True))

    value = deepcopy(chain)
    value[1], value[2] = value[2], value[1]
    out.append(("reorder", value, True))

    value = deepcopy(chain)
    value[2]["trace_id"] = "trace-attacker"
    out.append(("trace_substitution", value, True))

    value = deepcopy(chain)
    value[2]["previous_hash"] = "0" * 64
    out.append(("link_substitution", value, True))

    value = deepcopy(chain)
    del value[1]
    out.append(("middle_deletion", value, True))

    value = deepcopy(chain)
    value[2]["evidence"]["task_id"] = "forged-task"
    out.append(("evidence_insertion", value, True))

    # A self-contained hash chain cannot detect an attacker deleting only its tail
    # unless a trusted external system stores the expected head/count.
    value = deepcopy(chain[:-1])
    out.append(("tail_truncation_without_anchor", value, False))
    return out


def main() -> None:
    chain = base_chain()
    rows = []
    for name, mutated, expected_detected in mutations(chain):
        detected = not verify_audit_chain(mutated)
        rows.append({
            "mutation": name,
            "expected_detected": expected_detected,
            "detected": detected,
            "correct": detected == expected_detected,
        })
    result = {
        "base_chain_valid": verify_audit_chain(chain),
        "mutations": rows,
        "detectable_mutations": sum(row["expected_detected"] for row in rows),
        "detected_mutations": sum(row["detected"] for row in rows),
        "expectation_accuracy": sum(row["correct"] for row in rows) / len(rows),
        "known_limitation": "Tail truncation is not detectable without an external anchor.",
    }
    output = Path(__file__).with_name("results") / "audit_summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
