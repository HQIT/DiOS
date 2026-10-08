"""Read-only local evidence inventory. Never export event bodies or credentials."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, file_hash


def load(relative):
    path = ROOT / relative
    return json.loads(path.read_text(encoding="utf-8")), {"path": relative, "sha256": file_hash(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("New output required")
    frozen = ROOT / "experiments/e2ag/frozen_cases.jsonl"
    review, review_ref = load("experiments/e2ag/review/independent_review_panel_summary.json")
    mutations, mutation_ref = load("experiments/e2ag/results/mutation_summary.json")
    e2e, e2e_ref = load("experiments/e2ag/results/e2e_chain_summary.json")
    llm, llm_ref = load("experiments/e2ag/results/live_llm_chain_summary.json")
    field, field_ref = load("experiments/e2ag/results/external_qame_demogo_summary.json")
    regression, regression_ref = load("experiments/e2ag/results/external_governance_regression_summary.json")
    entries = [
        {"kind": "handcrafted_frozen_matrix", "cases": len(frozen.read_text(encoding="utf-8").splitlines()),
         "reviewers": review["reviewer_count"], "reviewed_cases": review["cases"],
         "source": {"path": str(frozen.relative_to(ROOT)), "sha256": file_hash(frozen)}, "review_source": review_ref,
         "reusable_for": "mechanism coverage and label-review evidence", "independent_natural_holdout": False},
        {"kind": "synthetic_mutations", "base_cases": mutations["base_benign_cases"],
         "generated_cases": mutations["synthetic_cases"], "operators": len(mutations["operators"]), "source": mutation_ref,
         "reusable_for": "robustness of authored rule families", "independent_natural_holdout": False},
        {"kind": "deterministic_e2e", "scenarios": len(e2e["scenarios"]), "executions": e2e["executions"] if "executions" in e2e else None,
         "source": e2e_ref, "reusable_for": "host execution/ablation regression", "independent_natural_holdout": False},
        {"kind": "prior_model_outputs", "scenarios": len(llm["scenarios"]), "model_calls": llm["model_calls"],
         "executions": llm["executions"], "source": llm_ref, "reusable_for": "fixed model-output replay; no fresh model requests",
         "independent_natural_holdout": False},
        {"kind": "field_positive_trace", "trace_count": 1, "tool_calls": len(field["e2ag"]["tool_calls"]), "source": field_ref,
         "reusable_for": "previously seen field-chain deployment evidence", "independent_natural_holdout": False},
        {"kind": "field_fixed_negative_control", "negative_count": 1, "source": regression_ref,
         "positive_references_same_trace": regression["positive_control"]["event_id"] == field["delivery"]["isolated_signed_replay_event_id"],
         "reusable_for": "owned-facility deterministic governance regression", "independent_natural_holdout": False},
    ]
    databases = []
    for relative in ("backend/dios.db", "backend/e2ag-api-smoke.db"):
        path = ROOT / relative
        item = {"path": relative, "exists": path.exists(), "mode": "SQLite mode=ro + query_only; no payload export"}
        if path.exists():
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                item["event_table_present"] = "event_logs" in tables
                if "event_logs" in tables:
                    item["event_count"] = conn.execute("SELECT COUNT(*) FROM event_logs").fetchone()[0]
                    item["groups"] = [{"source": r[0], "type": r[1], "count": r[2]}
                                      for r in conn.execute("SELECT source,event_type,COUNT(*) FROM event_logs GROUP BY source,event_type")]
                else:
                    item["event_count"] = 0
                item["natural_provenance_verified"] = False
            finally:
                conn.close()
        databases.append(item)
    result = {"scope": "local known paper evidence and two named local databases; remote inventory not performed",
              "entries": entries, "local_databases": databases,
              "verified_unused_natural_holdout_count": 0,
              "warning": "Zero verified is not a claim that no events exist remotely. Human review of synthetic cases does not make them natural traffic.",
              "next_collection": {"read_only_inventory_first": True, "fields": ["source/type aggregate counts", "time range", "unique delivery IDs", "workflows/tools represented"],
                                  "exclude": ["experimental branch/control events", "duplicate/redelivered event IDs", "already used calibration traces"],
                                  "separate": ["natural base events", "controlled negative variants", "repeats"],
                                  "no_event_bodies_or_secrets_at_inventory_stage": True},
              "script_sha256": file_hash(Path(__file__))}
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({"evidence_groups": len(entries), "local_databases": databases,
                      "verified_unused_natural_holdout_count": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
