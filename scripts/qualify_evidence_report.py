"""Report qualification; reuse unchanged execution/accounting evidence."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("DIOS_TEST_POSTGRES_URL"):
        parser.error("isolated DIOS_TEST_POSTGRES_URL required")
    paths = list((ROOT / "backend/app/runtime").glob("*.py")) + [
        ROOT / "backend/tests/test_evidence_report.py",
        ROOT / "scripts/report_runtime_evidence.py",
        ROOT / "scripts/probe_evidence_boundaries.py",
    ]
    hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }
    started = time.monotonic()
    suites = ["test_evidence_report"]
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(suites)
    )
    report = {
        "work_item": "K04-3.04",
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "seconds": time.monotonic() - started,
        "source_sha256": hashes,
        "suites": suites,
        "passed": result.wasSuccessful()
        and not result.skipped
        and all(
            hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == s
            for p, s in hashes.items()
        ),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: report[k] for k in ("passed", "tests", "failures", "errors", "skipped")}
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
