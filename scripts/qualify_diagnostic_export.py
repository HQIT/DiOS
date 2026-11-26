"""Execution evidence read paths across SQLite, PostgreSQL, API, CLI and SDK."""

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
sys.path[:0] = [
    str(ROOT / "backend"),
    str(ROOT / "backend/tests"),
    str(ROOT / "sdk/python/src"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("DIOS_TEST_POSTGRES_URL"):
        parser.error("An isolated DIOS_TEST_POSTGRES_URL is required")
    suites = [
        "test_diagnostic_export",
        "test_run_evidence",
        "test_runtime_diagnostics",
        "test_runtime_principals",
        "test_runtime_object_access.ObjectAccessTests",
        "test_runtime_task_console.TaskConsoleTests",
        "test_execution_revisions",
    ]
    paths = (
        list((ROOT / "backend/app/runtime").glob("*.py"))
        + [
            ROOT / "backend/app/services/run_evidence.py",
            ROOT / "backend/app/services/object_access.py",
            ROOT / "backend/app/services/task_console.py",
            ROOT / "backend/app/api/os/runtime_tasks.py",
            ROOT / "sdk/python/src/dios_sdk/tasks.py",
            ROOT / "sdk/python/src/dios_sdk/__init__.py",
            ROOT / "sdk/python/pyproject.toml",
            ROOT / "frontend/src/apps/console/components/RunEvidencePanel.tsx",
            ROOT / "frontend/src/apps/console/components/TaskRunsPage.tsx",
            ROOT / "frontend/src/api/runtime.ts",
            ROOT / "frontend/src/lib/messages.ts",
        ]
        + [ROOT / "backend/tests" / (suite.split(".")[0] + ".py") for suite in suites]
    )
    hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }
    os.chdir(ROOT / "backend")
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(suites)
    )
    unchanged = all(
        hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == sha
        for p, sha in hashes.items()
    )
    report = dict(
        work_item="K04-3.02",
        complete=False,
        passed=result.wasSuccessful() and not result.skipped and unchanged,
        base_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        tests=result.testsRun,
        errors=len(result.errors),
        failures=len(result.failures),
        skipped=len(result.skipped),
        seconds=round(time.monotonic() - started, 3),
        suites=suites,
        source_sha256=hashes,
        limitations=[
            "Bounded support projection; not a complete audit log or backup.",
            "Scheduler and receiver health is operator-only; unregistered remote receivers are unknown.",
            "Digests verify package bytes, not the original evidence chain.",
        ],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ["passed", "tests", "errors", "failures", "skipped"]
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
