"""Registry acceptance: consumer gates, release lifecycle and endpoint review."""

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


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("DIOS_TEST_DOCKER"):
        parser.error("DIOS_TEST_DOCKER=1 is required for native Registry qualification")
    os.chdir(ROOT / "backend")
    from test_package_registry import OBSERVATIONS

    suites = [
        "test_package_execution_entries",
        "test_package_lock.PackageLockTests",
        "test_package_lock.PackagePublishGateTests",
        "test_package_admission",
        "test_package_registry",
        "test_package_catalog.PackageCatalogTests",
        "test_execution_revisions.SQLiteBindingTests",
        "test_runtime_task_console.TaskConsoleTests",
        "test_deployment_console.DeploymentConsoleTests",
        "test_runtime_worker.WorkerTests",
        "test_endpoint_compatibility.SQLiteCompatibilityTests",
        "test_endpoint_compatibility.RemoteCompatibilityTests",
    ]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(suites)
    )
    # Reuse UI/SDK evidence only if all paths those probes exercise are unchanged.
    catalog_path = ROOT / "docs/design/evidence/package-catalog-20261126.json"
    catalog = json.loads(catalog_path.read_text())
    reuse = {
        path: sha(ROOT / path) == checksum
        for path, checksum in {
            **catalog["source_sha256"],
            **catalog["additional_source_sha256"],
        }.items()
    }
    sources = [path for path in (ROOT / "backend/app/runtime").glob("*.py")] + [
        ROOT / "backend/app/services/task_console.py",
        Path(__file__).resolve(),
    ]
    sources += [
        ROOT / "backend/tests" / (name + ".py")
        for name in {suite.split(".")[0] for suite in suites}
    ]
    report = {
        "work_item": "K04-1.05",
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "passed": result.wasSuccessful()
        and not result.skipped
        and len(OBSERVATIONS) == 6
        and all(reuse.values()),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "seconds": round(time.monotonic() - started, 3),
        "suites": suites,
        "native_execution": OBSERVATIONS,
        "reused_catalog": {
            "evidence": str(catalog_path.relative_to(ROOT)),
            "evidence_sha256": sha(catalog_path),
            "unchanged_sources": reuse,
            "browser_checks": catalog["browser"],
            "api_cli_sdk_checks": catalog["sdk"],
            "wheel": catalog["wheel"],
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): sha(path) for path in sorted(set(sources))
        },
        "acceptance": {
            "K04-1-A01": "Exact digest/dependency binding; pending, rejected, revoked and evidence-invalid packages refused; legacy CLI/Console/Worker package paths require Revision.",
            "K04-1-A02": "Six real Docker runs exercise initial install, failed upgrade, accepted upgrade, retained old image, stale rollback approval rejection and freshly authorized rollback.",
            "K04-1-A03": "Shared API/CLI/SDK catalog contracts; unchanged 16-check browser evidence; package export is independent of deployment activation.",
            "K04-1-A04": "Current admission evidence is checked at publication, Work binding and E2AG checkpoints; endpoint version/capability/protocol changes and restoration require review; original task recovery remains possible.",
        },
        "limits": [
            "Trusted Linux publisher and reviewer; metadata review does not implement signature trust-root verification or SLSA certification.",
            "External services can change implementation without changing their declarations; such changes are not detectable by declaration comparison alone.",
            "Remote model implementation rollback is not available; local image/configuration rollback and retained task identity are supported.",
            "External peer tests are synthetic; no new commercial-model qualification is claimed.",
            "Package transport uses existing Objects and OCI image publication; no OCI Distribution HTTP registry server is provided.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "passed",
                    "tests",
                    "failures",
                    "errors",
                    "skipped",
                    "seconds",
                )
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
