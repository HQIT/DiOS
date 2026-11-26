"""Unified catalog contracts and affected operator API regression."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.chdir(ROOT / "backend")
    suites = [
        "test_package_catalog.PackageCatalogTests",
        "test_package_registry.RegistryTests",
        "test_runtime_task_console.TaskConsoleTests",
        "test_deployment_console.DeploymentConsoleTests",
    ]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(suites)
    )
    paths = [
        "backend/app/runtime/package_catalog.py",
        "backend/app/runtime/cli.py",
        "backend/app/api/os/runtime_packages.py",
        "backend/app/api/os/runtime_tasks.py",
        "backend/tests/test_package_catalog.py",
        "sdk/python/src/dios_sdk/tasks.py",
        "frontend/src/api/runtime.ts",
        "frontend/src/apps/console/components/PackageCatalogPage.tsx",
        "frontend/src/apps/console/components/RegistryPage.tsx",
        "frontend/src/apps/console/components/package-catalog.css",
        "frontend/src/lib/messages.ts",
        "scripts/qualify_package_catalog.py",
        "scripts/serve_package_catalog_fixture.py",
        "scripts/probe_package_catalog_browser.mjs",
        "scripts/probe_package_catalog_sdk.py",
    ]
    report = {
        "work_item": "K04-1.04",
        "passed": result.wasSuccessful() and not result.skipped,
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "seconds": round(time.monotonic() - started, 3),
        "suites": suites,
        "source_sha256": {
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths
        },
        "limits": [
            "Catalog API is restricted to existing platform-operator credentials; application end users cannot browse global package metadata.",
            "Admission filters select the latest recorded decision; current evidence validity and execution conditions are separate explicit fields.",
            "Revision associations use bounded candidate pages; a nonempty continuation requires another request even when a page has no matching items.",
        ],
    }
    args.output.parent.mkdir(exist_ok=True, parents=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("passed", "tests", "failures", "errors", "skipped", "seconds")
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
