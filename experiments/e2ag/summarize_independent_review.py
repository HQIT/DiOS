"""Validate a completed blinded review and report inter-rater agreement."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
REVIEW_DIR = HERE / "review"
FIELDS = (
    ("reviewer_class", "author_class", {"benign", "attack"}),
    ("reviewer_governance_layer", "author_layer", {"benign", "contract", "policy", "approval"}),
    ("reviewer_expected_enforce_decision", "author_expected_enforce_decision", {"allow", "deny", "approval_required"}),
)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def cohen_kappa(left: list[str], right: list[str]) -> tuple[float, float]:
    count = len(left)
    if count == 0 or count != len(right):
        raise ValueError("agreement vectors must be non-empty and equal length")
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / count
    left_counts, right_counts = Counter(left), Counter(right)
    expected = sum(left_counts[label] * right_counts[label] for label in set(left) | set(right)) / (count * count)
    kappa = (1.0 if observed == 1.0 else 0.0) if expected == 1.0 else (observed - expected) / (1.0 - expected)
    return observed, kappa


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", type=Path, default=REVIEW_DIR / "frozen_review_blind.csv")
    parser.add_argument("--map", dest="mapping", type=Path, default=REVIEW_DIR / "frozen_review_author_map.csv")
    parser.add_argument("--output-dir", type=Path, default=REVIEW_DIR)
    args = parser.parse_args()
    review_rows, map_rows = read_rows(args.review), read_rows(args.mapping)
    if len(review_rows) != 60 or len(map_rows) != 60:
        raise ValueError("both review and mapping files must contain exactly 60 cases")
    review_by_id = {row["review_id"]: row for row in review_rows}
    map_by_id = {row["review_id"]: row for row in map_rows}
    if set(review_by_id) != set(map_by_id):
        raise ValueError("review ids do not match the author mapping")

    missing, invalid = [], []
    for review_id, row in review_by_id.items():
        for reviewer_field, _, allowed in FIELDS:
            value = row.get(reviewer_field, "").strip()
            if not value:
                missing.append(f"{review_id}:{reviewer_field}")
            elif value not in allowed:
                invalid.append(f"{review_id}:{reviewer_field}={value}")
        confidence = row.get("reviewer_confidence_1_5", "").strip()
        if not confidence:
            missing.append(f"{review_id}:reviewer_confidence_1_5")
        elif confidence not in {"1", "2", "3", "4", "5"}:
            invalid.append(f"{review_id}:reviewer_confidence_1_5={confidence}")
        for metadata in ("reviewer_name", "reviewed_at"):
            if not row.get(metadata, "").strip():
                missing.append(f"{review_id}:{metadata}")
    if missing or invalid:
        raise ValueError(
            f"review is incomplete: missing={len(missing)}, invalid={len(invalid)}; examples={(missing + invalid)[:8]}"
        )

    summary: dict[str, object] = {
        "protocol": "independent-frozen-matrix-review-v1",
        "cases": 60,
        "reviewer_names": sorted({row["reviewer_name"].strip() for row in review_rows}),
        "reviewed_at_values": sorted({row["reviewed_at"].strip() for row in review_rows}),
        "agreement": {},
    }
    disagreements: list[dict[str, str]] = []
    for reviewer_field, author_field, _ in FIELDS:
        keys = sorted(review_by_id)
        reviewer_values = [review_by_id[key][reviewer_field].strip() for key in keys]
        author_values = [map_by_id[key][author_field].strip() for key in keys]
        observed, kappa = cohen_kappa(reviewer_values, author_values)
        summary["agreement"][reviewer_field] = {
            "raw_agreement": round(observed, 6),
            "cohen_kappa": round(kappa, 6),
            "agreements": sum(a == b for a, b in zip(reviewer_values, author_values, strict=True)),
        }
    for review_id in sorted(review_by_id):
        review, author = review_by_id[review_id], map_by_id[review_id]
        if any(review[r].strip() != author[a].strip() for r, a, _ in FIELDS):
            disagreements.append({
                "review_id": review_id,
                "case_id": author["case_id"],
                "author_class": author["author_class"],
                "reviewer_class": review["reviewer_class"],
                "author_layer": author["author_layer"],
                "reviewer_layer": review["reviewer_governance_layer"],
                "author_decision": author["author_expected_enforce_decision"],
                "reviewer_decision": review["reviewer_expected_enforce_decision"],
                "reviewer_notes": review["reviewer_notes"],
                "final_resolution": "", "resolution_reason": "", "resolved_by": "", "resolved_at": "",
            })
    summary["disagreement_cases"] = len(disagreements)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.output_dir / "independent_review_summary.json"
    disagreements_path = args.output_dir / "independent_review_disagreements.csv"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = list(disagreements[0]) if disagreements else [
        "review_id", "case_id", "final_resolution", "resolution_reason", "resolved_by", "resolved_at"
    ]
    with disagreements_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(disagreements)
    print(summary_path)
    print(disagreements_path)


if __name__ == "__main__":
    main()
