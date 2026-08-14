"""Create a blinded review sheet for the frozen E2AG threat matrix."""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "frozen_cases.jsonl"
REVIEW_DIR = HERE / "review"
SEED = 20260814


def expected_decision(case: dict) -> str:
    if case["layer"] == "benign":
        return "allow"
    if case["layer"] == "approval":
        return "approval_required"
    return "deny"


def main() -> None:
    cases = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(cases) != 60:
        raise ValueError(f"expected 60 frozen cases, found {len(cases)}")
    random.Random(SEED).shuffle(cases)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    blind_path = REVIEW_DIR / "frozen_review_blind.csv"
    mapping_path = REVIEW_DIR / "frozen_review_author_map.csv"
    blind_fields = [
        "review_id", "event_source", "event_type", "requested_action", "requested_tool", "environment",
        "target_governance_json", "reviewer_class", "reviewer_governance_layer",
        "reviewer_expected_enforce_decision", "reviewer_confidence_1_5", "reviewer_notes",
        "reviewer_name", "reviewed_at",
    ]
    map_fields = [
        "review_id", "case_id", "scenario", "author_class", "author_layer",
        "author_expected_enforce_decision",
    ]
    with blind_path.open("w", newline="", encoding="utf-8-sig") as blind_stream, mapping_path.open(
        "w", newline="", encoding="utf-8-sig"
    ) as map_stream:
        blind_writer = csv.DictWriter(blind_stream, fieldnames=blind_fields, lineterminator="\n")
        map_writer = csv.DictWriter(map_stream, fieldnames=map_fields, lineterminator="\n")
        blind_writer.writeheader()
        map_writer.writeheader()
        for index, case in enumerate(cases, start=1):
            review_id = f"R{index:03d}"
            event = case["event"]
            data = event.get("data") or {}
            blind_writer.writerow({
                "review_id": review_id,
                "event_source": event.get("source", ""),
                "event_type": event.get("type", ""),
                "requested_action": data.get("requested_action", ""),
                "requested_tool": data.get("requested_tool", ""),
                "environment": data.get("environment", ""),
                "target_governance_json": json.dumps(
                    case.get("targets", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "reviewer_class": "",
                "reviewer_governance_layer": "",
                "reviewer_expected_enforce_decision": "",
                "reviewer_confidence_1_5": "",
                "reviewer_notes": "",
                "reviewer_name": "",
                "reviewed_at": "",
            })
            map_writer.writerow({
                "review_id": review_id,
                "case_id": case["id"],
                "scenario": case["scenario"],
                "author_class": case["class"],
                "author_layer": case["layer"],
                "author_expected_enforce_decision": expected_decision(case),
            })
    print(blind_path)
    print(mapping_path)


if __name__ == "__main__":
    main()
