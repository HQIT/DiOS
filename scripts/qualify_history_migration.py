"""Qualify legacy schema migration, immutable import, and relation queries."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.chdir(ROOT / "backend")
    from tests.test_history_migration_qualification import OBSERVATIONS

    names = [
        "tests.test_history_migration_qualification.CrashHistoryQualification",
        "tests.test_history_migration_qualification.SourceExtensionQualification",
        "tests.test_runtime_history.SQLiteHistory",
        "tests.test_runtime_history.HistoryMigrationTests",
        "tests.test_managed_history.ManagedHistoryTests",
    ]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(names)
    )
    paths = [
        "backend/tests/test_history_migration_qualification.py",
        "backend/tests/test_runtime_history.py",
        "backend/tests/test_managed_history.py",
        "backend/app/runtime/database.py",
        "backend/app/runtime/history.py",
        "backend/app/runtime/history_archive.py",
        "backend/app/services/managed_history.py",
        "backend/app/runtime/schema.py",
        "backend/app/runtime/history_schema.py",
        "backend/app/runtime/history_archive_schema.py",
        "scripts/qualify_history_migration.py",
    ]
    report = {
        "work_item": "K02-2.01",
        "passed": result.wasSuccessful()
        and not result.skipped
        and len(OBSERVATIONS) == 3,
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "seconds": round(time.monotonic() - started, 3),
        "selected_suites": names,
        "implementation_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "observations": OBSERVATIONS,
        "source_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths
        },
        "failure_cases": [
            {"test": case.id(), "traceback": trace}
            for case, trace in result.errors + result.failures
        ],
        "limits": [
            "Isolated Linux SQLite databases and synthetic source records; no production review migration.",
            "Native runtime v1 and managed-history source adapter; external Codeup source schema requires its own adapter qualification.",
            "SIGKILL process interruption is tested; physical host power loss and storage hardware durability are separate qualifications.",
            "PostgreSQL provider parity and multi-node consistency belong to K02-2.03 and W4.",
        ],
        "references": [
            "https://www.sqlite.org/atomiccommit.html",
            "https://www.sqlite.org/backup.html",
        ],
    }
    destination = args.output if args.output.is_absolute() else ROOT / args.output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in ["passed", "tests", "failures", "errors", "skipped", "seconds"]
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
