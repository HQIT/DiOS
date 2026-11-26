"""Bounded package and affected Revision/Worker/Deployment regression evidence."""

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
    from test_package_lock import OBSERVATIONS

    suites = [
        "test_package_lock",
        "test_execution_revisions.SQLiteBindingTests",
        "test_runtime_revisions.SQLiteRevisionTests",
        "test_runtime_revisions.ManifestTests",
        "test_runtime_deployments.SQLiteDeploymentTests",
        "test_revision_mounts",
        "test_runtime_worker.WorkerTests",
        "test_runtime_worker.BridgeTests",
    ]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(suites)
    )
    paths = [
        "backend/app/runtime/" + name + ".py"
        for name in (
            "package_lock",
            "package_payloads",
            "execution_revisions",
            "worker",
            "cli",
            "revision_mounts",
            "deployments",
        )
    ]
    paths += [
        "backend/tests/test_package_lock.py",
        "backend/requirements.txt",
        "scripts/qualify_package_lock.py",
    ]
    evidence = {
        "work_item": "K04-1.01",
        "passed": result.wasSuccessful()
        and not result.skipped
        and len(OBSERVATIONS) == 2,
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
            "Local Linux OCI publication; package sources are supplied by a trusted publisher.",
            "PEP 440 versions for DiOS package metadata; native ecosystem installation belongs to package producers.",
            "Source/licence admission and Registry catalog UI are subsequent K04-1.02/.03/.04 deliveries.",
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
