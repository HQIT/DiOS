"""Verify all pre-frozen 42 cases, retaining original and continued locations."""
import argparse
import hashlib
import json
from pathlib import Path

from check_paired_matrix import audit_case, load


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--continued", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original, continued = args.original.resolve(), args.continued.resolve()
    freeze = load(original / "freeze.json")
    continuation = load(continued / "continuation.json")
    assert continuation["original_freeze_sha256"] == hashlib.sha256((original / "freeze.json").read_bytes()).hexdigest()
    assert load(continued / "summary.json")["changed_original_sources"] == []
    common = {"normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"}
    expected = {(config, mode) for config in ["R0", "R1", "R2", "R3", "R4"] for mode in common} | {("R5", "J01"), ("R5", "J02")}
    assert {(c["config"], c["case"]) for c in freeze["cases"]} == expected
    rows, failures = [], []
    for case in freeze["cases"]:
        config, name = case["config"], case["case"]
        folder = (continued if config == "R5" else original) / config / name
        try:
            problems, raw = audit_case(folder, case["mode"], *freeze["configs"][config])
        except Exception as exc:
            problems, raw = [type(exc).__name__ + ":" + str(exc)], {}
        failures += [config + "/" + name + ":" + p for p in problems]
        rows.append({**case, "artifact_directory": str(folder), "problems": problems,
            "counts": {k: len(v) for k, v in raw.items()}})
    result = {"status": "failed" if failures else "passed", "case_count": len(rows), "failures": failures,
        "cases": rows, "source_freeze": str(original / "freeze.json"), "continuation": str(continued / "continuation.json"),
        "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "case_checker_sha256": hashlib.sha256(Path(__file__).with_name("check_paired_matrix.py").read_bytes()).hexdigest()}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "case_count": len(rows), "failures": failures}))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
