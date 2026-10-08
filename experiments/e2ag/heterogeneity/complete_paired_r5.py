"""Complete only the two pre-frozen R5 cases after management-channel timeout.

All files in the original source freeze must remain identical. Existing 40
main cases are not rerun, replaced or copied. The original failure is retained.
"""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import traceback

from run_file_recovery import ROOT, save_new
from run_amqp_qualification import broker, run_case
from postgres_runtime import postgres


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = args.original.resolve()
    freeze = json.loads((original / "freeze.json").read_text(encoding="utf-8"))
    assert freeze["kind"] == "paired_matrix" and len(freeze["cases"]) == 42
    for path, expected in freeze["source_sha256"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
    for config in ["R0", "R1", "R2", "R3", "R4"]:
        summary = json.loads((original / config / "summary.json").read_text(encoding="utf-8"))
        assert summary["case_count"] == 8 and all(c["passed"] for c in summary["cases"])
    assert not list((original / "R5/J01").iterdir()), "R5 must not have sent any input"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = [c for c in freeze["cases"] if c["config"] == "R5"]
    save_new(output / "continuation.json", {"created_at": datetime.now(timezone.utc).isoformat(),
        "original": str(original), "original_freeze_sha256": hashlib.sha256((original / "freeze.json").read_bytes()).hexdigest(),
        "cases": cases, "source_sha256": freeze["source_sha256"], "all_original_sources_unchanged_before": True,
        "reason": "idle broker administration connection timed out before R5 queue creation; no R5 worker or publication",
        "completion_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    rows = []
    with ExitStack() as stack:
        for subdir in ["broker", "postgres", "R5"]: (output / subdir).mkdir()
        port, channel, environment, password = stack.enter_context(broker(output / "broker"))
        pg = stack.enter_context(postgres(output / "postgres"))
        for case in cases:
            folder = output / "R5" / case["case"]
            name = "e2ag_r5_" + case["case"].lower()
            url = asyncio.run(pg.create(name))
            try:
                row = run_case(folder, case["mode"], case["cut"], port, channel, environment, password,
                               tool_transport="stdio", database_url=url)
            except Exception:
                row = {"case": case["case"], "passed": False, "error": traceback.format_exc().replace(pg.password, "[REDACTED]")}
            save_new(output / "R5" / (case["case"] + ".json"), row)
            try:
                asyncio.run(pg.export(name, folder))
            except Exception:
                save_new(folder / "export-error.json", {"error": traceback.format_exc().replace(pg.password, "[REDACTED]")})
                row["passed"] = False
            rows.append({"case": case["case"], "passed": row["passed"]})
            print(json.dumps(rows[-1]), flush=True)
            if not row["passed"]: break
    changed = [path for path, expected in freeze["source_sha256"].items()
               if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected]
    passed = len(rows) == 2 and all(r["passed"] for r in rows) and not changed
    save_new(output / "summary.json", {"status": "passed" if passed else "failed", "cases": rows,
        "changed_original_sources": changed})
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
