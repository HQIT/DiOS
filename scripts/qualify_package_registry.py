"""Registry publication and affected release/recovery qualification."""

import argparse
import hashlib
import json
import os
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
    from test_package_registry import OBSERVATIONS

    suites = [
        "test_package_registry",
        "test_package_admission",
        "test_runtime_deployments.SQLiteDeploymentTests",
        "test_execution_revisions.SQLiteBindingTests",
        "test_runtime_recovery.SQLiteDrain",
        "test_runtime_recovery.RecoveryTests",
        "test_backup_inventory",
    ]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(suites)
    )
    paths = [
        "backend/app/runtime/" + name + ".py"
        for name in (
            "package_registry",
            "package_registry_schema",
            "package_admission",
            "package_lock",
            "package_payloads",
            "database",
            "recovery",
            "deployments",
            "cli",
            "execution_revisions",
        )
    ] + [
        "backend/tests/test_package_registry.py",
        "scripts/qualify_package_registry.py",
    ]
    evidence = {
        "work_item": "K04-1.03",
        "passed": result.wasSuccessful()
        and not result.skipped
        and len(OBSERVATIONS) == 6,
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "seconds": round(time.monotonic() - started, 3),
        "suites": suites,
        "native_execution": OBSERVATIONS,
        "source_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths
        },
        "limits": [
            "Trusted Linux CLI Registry; remote API and Console catalog are K04-1.04.",
            "Explicit locked package closure; target environments require their own admission evidence and E2AG policy.",
            "Content distribution uses existing Objects/ArtifactProvider; OCI images use the existing Revision publisher. OCI Distribution HTTP compatibility is not implemented.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: evidence[k]
                for k in ("passed", "tests", "failures", "errors", "skipped", "seconds")
            }
        )
    )
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
