"""Opt-in native Linux Docker qualification of the strict execution policy."""

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
    if os.environ.get("DIOS_TEST_DOCKER") != "1":
        parser.error("Set DIOS_TEST_DOCKER=1 for isolated native qualification")
    from test_runtime_execution_isolation import NATIVE_OBSERVATIONS

    suites = [
        "test_runtime_execution_isolation",
        "test_runtime_worker",
        "test_runtime_worker_tool_channel",
        "test_runtime_tool_budgets.SQLiteToolBudgetTests",
        "test_runtime_tool_budgets.HttpToolBudgetTests",
        "test_execution_revisions.SQLiteBindingTests",
        "test_revision_mounts",
        "test_runtime_task_console.TaskConsoleTests",
    ]
    paths = (
        list((ROOT / "backend/app/runtime").glob("*.py"))
        + [
            ROOT / "backend/app/services/task_console.py",
            ROOT / "frontend/src/api/runtime.ts",
            ROOT / "frontend/src/apps/console/components/TaskRunsPage.tsx",
            ROOT / "frontend/src/lib/messages.ts",
        ]
        + [ROOT / "backend/tests" / (name.split(".")[0] + ".py") for name in suites]
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
        hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == value
        for p, value in hashes.items()
    )
    report = dict(
        work_item="K04-2.03",
        complete=False,
        base_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        passed=result.wasSuccessful()
        and not result.skipped
        and unchanged
        and len(NATIVE_OBSERVATIONS) == 1,
        tests=result.testsRun,
        errors=len(result.errors),
        failures=len(result.failures),
        skipped=len(result.skipped),
        seconds=round(time.monotonic() - started, 3),
        suites=suites,
        native=NATIVE_OBSERVATIONS,
        source_sha256=hashes,
        qualified_policy={"version": 1, "network": "none", "workspace": "read_only"},
        limitations=[
            "Local Linux Docker daemon; remote execution uses an enrolled Worker on that host.",
            "Direct model network access and remote A2A endpoints cannot satisfy this strict profile.",
            "Trusted operator-owned image, source mounts and host; writable /tmp is bounded to 64 MiB.",
        ],
    )
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
