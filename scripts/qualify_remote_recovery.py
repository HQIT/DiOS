"""Qualify recovery with native mTLS Workers and focused governance contracts."""

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
    os.chdir(ROOT)
    os.environ["DIOS_TEST_DOCKER"] = "1"
    from test_remote_recovery_qualification import OBSERVATIONS

    names = [
        "test_remote_recovery_qualification.RemoteRecoveryQualificationTests",
        "test_runtime_faults.FaultTests",
        "test_runtime_write_queue.WriteGateTests",
        "test_runtime_write_queue.RuntimeWriteTests",
        "test_runtime_remote_control.RemoteControlTests.test_disconnected_result_recovers_after_worker_restart_without_relaunch",
        "test_runtime_remote_control.RemoteControlTests.test_completed_response_loss_is_recovered_without_duplicate_artifacts",
        "test_runtime_remote_control.RemoteControlTests.test_remote_retry_uses_server_profile_and_same_work_identity",
        "test_runtime_remote_control.RemoteControlTests.test_e2ag_revocation_and_machine_revocation_reject_execution",
    ]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(names)
    )
    sources = [
        "backend/tests/test_remote_recovery_qualification.py",
        "backend/tests/test_runtime_faults.py",
        "backend/tests/test_runtime_remote_control.py",
        "backend/tests/test_runtime_write_queue.py",
        "backend/app/runtime/faults.py",
        "backend/app/runtime/remote_control.py",
        "backend/app/runtime/remote_runtime.py",
        "backend/app/runtime/store.py",
        "backend/app/runtime/database.py",
        "backend/app/runtime/write_queue.py",
    ]
    report = {
        "work_item": "K01-2.02",
        "passed": result.wasSuccessful()
        and len(OBSERVATIONS) == 2
        and not result.skipped,
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "duration_seconds": round(time.monotonic() - started, 3),
        "selected_suites": names,
        "implementation_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "native": OBSERVATIONS,
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in sources
        },
        "failure_cases": [
            {"test": case.id(), "traceback": trace}
            for case, trace in result.errors + result.failures
        ],
        "limits": [
            "One Linux/WSL host, SQLite controller, independent remote Worker processes over mTLS and native Docker tasks.",
            "Provider outages are explicit synthetic protocol errors; no actual model quota is consumed.",
            "FIFO fairness applies to one controller's bounded write queue; cross-controller/global scheduling fairness is not asserted.",
            "Physical multi-host loss and production capacity remain dedicated W4 qualifications.",
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
                    "duration_seconds",
                )
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
