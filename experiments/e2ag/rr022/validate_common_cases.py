"""Validate draft case structure only. Never executes any system under test."""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("common-cases-v0.1.json"))
    args = parser.parse_args()
    raw = args.cases.read_bytes()
    data = json.loads(raw)
    ids = set()
    for case in data["cases"]:
        assert case["id"] not in ids, "Duplicate case ID"
        ids.add(case["id"])
        assembled = deepcopy(data["base_input"])
        for key, value in case["overrides"].items():
            parts = key.split(".")
            obj = assembled
            for part in parts[:-1]:
                obj = obj[part]
            assert parts[-1] in obj, "Unexpected override field"
            obj[parts[-1]] = value
        assert "oracle" not in assembled
        assert case["oracle"]["receipt_count"] in {0, 1}
        assert set(case["oracle"]["decision_sequence"]) <= {"allow", "deny", "approval_required"}
        if case["cohort"] == "approval":
            assert case["oracle"]["pre_answer_receipts"] == 0
            assert assembled["approval"]["required"]
        if case["cohort"] == "event":
            assert case["oracle"]["created_tasks"] == 0
    print(json.dumps({"validation": "structure only, not experimental outcomes",
                      "protocol": data["protocol"], "case_count": len(ids),
                      "cohorts": dict(Counter(case["cohort"] for case in data["cases"])),
                      "sha256": hashlib.sha256(raw).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
