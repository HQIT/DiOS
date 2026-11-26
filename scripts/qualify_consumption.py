"""Consumption accounting qualification with SQLite, PostgreSQL and native Worker."""

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
    if not os.environ.get("DIOS_TEST_DOCKER") or not os.environ.get(
        "DIOS_TEST_POSTGRES_URL"
    ):
        parser.error("Set DIOS_TEST_DOCKER=1 and an isolated DIOS_TEST_POSTGRES_URL")
    from test_runtime_consumption import NATIVE_OBSERVATIONS

    os.chdir(ROOT / "backend")
    suites = [
        "test_runtime_consumption",
        "test_runtime_capacity.SQLiteCapacityTests",
        "test_runtime_execution_budgets.SQLiteBudgetTests",
        "test_runtime_worker.WorkerTests",
        "test_runtime_recovery.SQLiteDrain",
        "test_runtime_recovery.RecoveryTests",
        "test_backup_inventory",
        "test_runtime_task_console.TaskConsoleTests",
    ]
    paths = (
        list((ROOT / "backend/app/runtime").glob("*.py"))
        + [
            ROOT / "backend/app/services/task_console.py",
            ROOT / "backend/app/api/os/runtime_tasks.py",
            ROOT / "backend/app/api/os/runtime_consumption.py",
        ]
        + [ROOT / "backend/tests" / (suite.split(".")[0] + ".py") for suite in suites]
    )
    hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(suites)
    )
    unchanged = all(
        hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == checksum
        for p, checksum in hashes.items()
    )
    report = {
        "work_item": "K04-2.01",
        "complete": False,
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "passed": result.wasSuccessful()
        and not result.skipped
        and unchanged
        and len(NATIVE_OBSERVATIONS) == 1,
        "tests": result.testsRun,
        "errors": len(result.errors),
        "failures": len(result.failures),
        "skipped": len(result.skipped),
        "seconds": round(time.monotonic() - started, 3),
        "suites": suites,
        "native": NATIVE_OBSERVATIONS,
        "source_sha256": hashes,
        "limitations": [
            "Strict provider cost ceilings fail closed on unsupported channels. Settlement uses operator-supplied evidence; automatic provider billing is an optional follow-on adapter."
        ],
    }
    report["complete"] = report["passed"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("passed", "tests", "errors", "failures", "skipped")
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
