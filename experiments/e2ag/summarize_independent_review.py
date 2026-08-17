"""Validate blinded reviews and report author and inter-rater agreement."""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
REVIEW_DIR = HERE / "review"
INPUT_FIELDS = (
    "review_id",
    "event_source",
    "event_type",
    "requested_action",
    "requested_tool",
    "environment",
    "target_governance_json",
)
REVIEW_FIELDS = (
    "reviewer_class",
    "reviewer_governance_layer",
    "reviewer_expected_enforce_decision",
    "reviewer_confidence_1_5",
    "reviewer_notes",
    "reviewer_name",
    "reviewed_at",
)
FIELDS = (
    ("reviewer_class", "author_class", ("benign", "attack")),
    ("reviewer_governance_layer", "author_layer", ("benign", "contract", "policy", "approval")),
    (
        "reviewer_expected_enforce_decision",
        "author_expected_enforce_decision",
        ("allow", "deny", "approval_required"),
    ),
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_rows(path: Path) -> tuple[list[dict[str, str]], list[str], str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        try:
            raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "gb18030"
    reader = csv.DictReader(raw.decode(encoding).splitlines())
    if reader.fieldnames is None:
        raise ValueError(f"{path} has no CSV header")
    return list(reader), list(reader.fieldnames), encoding


def index_rows(rows: list[dict[str, str]], path: Path) -> dict[str, dict[str, str]]:
    keys = [row.get("review_id", "").strip() for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path} contains duplicate review_id values")
    if any(not key for key in keys):
        raise ValueError(f"{path} contains an empty review_id")
    return dict(zip(keys, rows, strict=True))


def cohen_kappa(left: list[str], right: list[str]) -> tuple[float, float]:
    count = len(left)
    if count == 0 or count != len(right):
        raise ValueError("agreement vectors must be non-empty and equal length")
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / count
    left_counts, right_counts = Counter(left), Counter(right)
    labels = set(left_counts) | set(right_counts)
    expected = sum(left_counts[label] * right_counts[label] for label in labels) / (count * count)
    kappa = (1.0 if observed == 1.0 else 0.0) if expected == 1.0 else (observed - expected) / (1.0 - expected)
    return observed, kappa


def fleiss_kappa(ratings: list[list[str]], labels: tuple[str, ...]) -> tuple[float, float | None]:
    if not ratings:
        raise ValueError("Fleiss kappa requires at least one item")
    rater_count = len(ratings[0])
    if rater_count == 1:
        return 1.0, None
    if any(len(item) != rater_count for item in ratings):
        raise ValueError("all items must have the same number of ratings")
    item_agreement: list[float] = []
    totals = Counter()
    for item in ratings:
        counts = Counter(item)
        totals.update(counts)
        item_agreement.append(
            (sum(counts[label] ** 2 for label in labels) - rater_count)
            / (rater_count * (rater_count - 1))
        )
    observed = sum(item_agreement) / len(item_agreement)
    denominator = len(ratings) * rater_count
    expected = sum((totals[label] / denominator) ** 2 for label in labels)
    kappa = (1.0 if observed == 1.0 else 0.0) if expected == 1.0 else (observed - expected) / (1.0 - expected)
    return observed, kappa


def validate_review(
    path: Path,
    rows: list[dict[str, str]],
    fieldnames: list[str],
    blind_by_id: dict[str, dict[str, str]],
    map_by_id: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    if fieldnames != list(INPUT_FIELDS + REVIEW_FIELDS):
        raise ValueError(f"{path} header differs from the frozen blind sheet")
    if len(rows) != 60:
        raise ValueError(f"{path} must contain exactly 60 cases, found {len(rows)}")
    by_id = index_rows(rows, path)
    if set(by_id) != set(blind_by_id) or set(by_id) != set(map_by_id):
        raise ValueError(f"{path} review ids do not match the frozen inputs")

    modified_inputs: list[str] = []
    missing: list[str] = []
    invalid: list[str] = []
    for review_id, row in by_id.items():
        blind = blind_by_id[review_id]
        for field in INPUT_FIELDS:
            if row.get(field, "") != blind.get(field, ""):
                modified_inputs.append(f"{review_id}:{field}")
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
        for metadata in ("reviewer_notes", "reviewer_name", "reviewed_at"):
            if not row.get(metadata, "").strip():
                missing.append(f"{review_id}:{metadata}")
    if modified_inputs or missing or invalid:
        raise ValueError(
            f"{path} validation failed: modified_inputs={len(modified_inputs)}, "
            f"missing={len(missing)}, invalid={len(invalid)}; "
            f"examples={(modified_inputs + missing + invalid)[:8]}"
        )
    reviewer_names = {row["reviewer_name"].strip() for row in rows}
    if len(reviewer_names) != 1:
        raise ValueError(f"{path} must contain exactly one reviewer identity")
    return by_id


def write_anonymized(
    path: Path,
    fieldnames: list[str],
    rows_by_id: dict[str, dict[str, str]],
    alias: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for review_id in sorted(rows_by_id):
            row = dict(rows_by_id[review_id])
            row["reviewer_name"] = alias
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", type=Path, action="append", default=[])
    parser.add_argument("--map", dest="mapping", type=Path, default=REVIEW_DIR / "frozen_review_author_map.csv")
    parser.add_argument("--blind", type=Path, default=REVIEW_DIR / "frozen_review_blind.csv")
    parser.add_argument("--output-dir", type=Path, default=REVIEW_DIR)
    parser.add_argument("--anonymized-dir", type=Path)
    args = parser.parse_args()
    review_paths = args.review or [args.blind]

    blind_rows, blind_header, _ = read_rows(args.blind)
    map_rows, _, _ = read_rows(args.mapping)
    if len(blind_rows) != 60 or len(map_rows) != 60:
        raise ValueError("the frozen blind sheet and author map must each contain exactly 60 cases")
    if blind_header != list(INPUT_FIELDS + REVIEW_FIELDS):
        raise ValueError("the frozen blind sheet header is not the expected protocol v1 header")
    blind_by_id = index_rows(blind_rows, args.blind)
    map_by_id = index_rows(map_rows, args.mapping)
    if set(blind_by_id) != set(map_by_id):
        raise ValueError("the frozen blind sheet ids do not match the author map")

    reviewers: list[dict[str, object]] = []
    keys = sorted(blind_by_id)
    for index, path in enumerate(review_paths):
        rows, fieldnames, encoding = read_rows(path)
        by_id = validate_review(path, rows, fieldnames, blind_by_id, map_by_id)
        alias = f"Reviewer {chr(ord('A') + index)}"
        confidences = [int(by_id[key]["reviewer_confidence_1_5"]) for key in keys]
        notes = [by_id[key]["reviewer_notes"].strip() for key in keys]
        reviewer: dict[str, object] = {
            "alias": alias,
            "source_sha256": file_sha256(path),
            "source_encoding": encoding,
            "reviewed_at_values": sorted({by_id[key]["reviewed_at"].strip() for key in keys}),
            "confidence": {
                "mean": round(sum(confidences) / len(confidences), 6),
                "distribution": dict(sorted(Counter(confidences).items())),
            },
            "notes": {
                "nonempty": sum(bool(note) for note in notes),
                "unique": len(set(notes)),
                "mean_characters": round(sum(len(note) for note in notes) / len(notes), 6),
            },
            "agreement_with_author": {},
            "rows": by_id,
        }
        for reviewer_field, author_field, _ in FIELDS:
            reviewer_values = [by_id[key][reviewer_field].strip() for key in keys]
            author_values = [map_by_id[key][author_field].strip() for key in keys]
            observed, kappa = cohen_kappa(reviewer_values, author_values)
            reviewer["agreement_with_author"][reviewer_field] = {
                "agreements": sum(a == b for a, b in zip(reviewer_values, author_values, strict=True)),
                "raw_agreement": round(observed, 6),
                "cohen_kappa": round(kappa, 6),
            }
        reviewer["joint_exact_agreement_with_author"] = sum(
            all(
                by_id[key][reviewer_field].strip() == map_by_id[key][author_field].strip()
                for reviewer_field, author_field, _ in FIELDS
            )
            for key in keys
        )
        reviewers.append(reviewer)
        if args.anonymized_dir:
            write_anonymized(
                args.anonymized_dir / f"reviewer-{chr(ord('a') + index)}.csv",
                fieldnames,
                by_id,
                alias,
            )

    panel_fields: dict[str, object] = {}
    pairwise_fields: dict[str, object] = {}
    for reviewer_field, _, labels in FIELDS:
        ratings = [
            [str(reviewer["rows"][key][reviewer_field]).strip() for reviewer in reviewers]
            for key in keys
        ]
        observed, kappa = fleiss_kappa(ratings, labels)
        panel_fields[reviewer_field] = {
            "unanimous_cases": sum(len(set(item)) == 1 for item in ratings),
            "raw_agreement": round(observed, 6),
            "fleiss_kappa": round(kappa, 6) if kappa is not None else None,
        }
        comparisons = []
        for left, right in itertools.combinations(reviewers, 2):
            left_values = [str(left["rows"][key][reviewer_field]).strip() for key in keys]
            right_values = [str(right["rows"][key][reviewer_field]).strip() for key in keys]
            pair_observed, pair_kappa = cohen_kappa(left_values, right_values)
            comparisons.append({
                "left": left["alias"],
                "right": right["alias"],
                "agreements": sum(a == b for a, b in zip(left_values, right_values, strict=True)),
                "raw_agreement": round(pair_observed, 6),
                "cohen_kappa": round(pair_kappa, 6),
            })
        pairwise_fields[reviewer_field] = comparisons

    note_overlap = []
    for left, right in itertools.combinations(reviewers, 2):
        exact_matches = sum(
            str(left["rows"][key]["reviewer_notes"]).strip()
            == str(right["rows"][key]["reviewer_notes"]).strip()
            for key in keys
        )
        note_overlap.append({"left": left["alias"], "right": right["alias"], "exact_row_matches": exact_matches})

    disagreements: list[dict[str, str]] = []
    for key in keys:
        for reviewer in reviewers:
            row = reviewer["rows"][key]
            author = map_by_id[key]
            if any(
                row[reviewer_field].strip() != author[author_field].strip()
                for reviewer_field, author_field, _ in FIELDS
            ):
                disagreements.append({
                    "review_id": key,
                    "case_id": author["case_id"],
                    "reviewer": str(reviewer["alias"]),
                    "author_class": author["author_class"],
                    "reviewer_class": row["reviewer_class"],
                    "author_layer": author["author_layer"],
                    "reviewer_layer": row["reviewer_governance_layer"],
                    "author_decision": author["author_expected_enforce_decision"],
                    "reviewer_decision": row["reviewer_expected_enforce_decision"],
                    "final_resolution": "",
                    "resolution_reason": "",
                    "resolved_by": "",
                    "resolved_at": "",
                })

    summary_reviewers = [
        {key: value for key, value in reviewer.items() if key != "rows"}
        for reviewer in reviewers
    ]
    summary = {
        "protocol": "independent-frozen-matrix-review-panel-v2",
        "cases": 60,
        "reviewer_count": len(reviewers),
        "input_integrity": {
            "all_review_ids_match": True,
            "modified_frozen_input_cells": 0,
            "missing_review_cells": 0,
            "invalid_review_cells": 0,
        },
        "independence_declaration": "reported_by_experiment_organizer",
        "reviewers": summary_reviewers,
        "panel_agreement": panel_fields,
        "pairwise_agreement": pairwise_fields,
        "pairwise_note_exact_matches": note_overlap,
        "unanimous_joint_tuple_cases": sum(
            all(
                len({str(reviewer["rows"][key][reviewer_field]).strip() for reviewer in reviewers}) == 1
                for reviewer_field, _, _ in FIELDS
            )
            for key in keys
        ),
        "disagreement_cases": len(disagreements),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.output_dir / "independent_review_panel_summary.json"
    disagreements_path = args.output_dir / "independent_review_disagreements.csv"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    disagreement_fields = [
        "review_id", "case_id", "reviewer", "author_class", "reviewer_class", "author_layer",
        "reviewer_layer", "author_decision", "reviewer_decision", "final_resolution",
        "resolution_reason", "resolved_by", "resolved_at",
    ]
    with disagreements_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=disagreement_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(disagreements)
    print(summary_path)
    print(disagreements_path)


if __name__ == "__main__":
    main()
