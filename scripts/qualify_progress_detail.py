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
    from test_progress_detail_qualification import OBSERVATIONS

    names = [
        "test_progress_detail_qualification.ProgressDetailQualification",
        "test_runtime_spool.SpoolTests",
        "test_runtime_persistence.SQLitePersistence.test_outbox_retry_ack_and_stale_projection",
        "test_runtime_persistence.ArtifactScenarios",
    ]
    names += [
        "test_runtime_remote_control.RemoteControlTests." + name
        for name in [
            "test_disconnected_result_recovers_after_worker_restart_without_relaunch",
            "test_completed_response_loss_is_recovered_without_duplicate_artifacts",
            "test_backup_contains_incomplete_upload_and_resumes",
            "test_e2ag_revocation_and_machine_revocation_reject_execution",
            "test_fenced_epoch_never_accepts_old_report",
        ]
    ]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(names)
    )
    paths = [
        "backend/tests/test_progress_detail_qualification.py",
        "backend/tests/test_runtime_spool.py",
        "backend/tests/test_runtime_persistence.py",
        "backend/tests/test_runtime_remote_control.py",
        "backend/app/runtime/remote_control.py",
        "backend/app/runtime/remote_runtime.py",
        "backend/app/runtime/remote_tool_channel.py",
        "backend/app/runtime/spool.py",
        "backend/app/runtime/reporting.py",
        "backend/app/runtime/store.py",
        "backend/app/runtime/artifacts.py",
        "scripts/qualify_progress_detail.py",
    ]
    report = {
        "work_item": "K02-2.02",
        "passed": result.wasSuccessful()
        and not result.skipped
        and len(OBSERVATIONS) == 1,
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
            "Real loopback mTLS with synthetic payload and authenticated E2AG-bound Attempt; no physical network partition or model service.",
            "Detail request stall is injected before its control transaction; independent progress does not imply availability during shared database or disk failure.",
            "3 MB individual artifact tested; current remote per-artifact limit is 8 MiB and per-attempt total 32 MiB. Larger business archives require bounded artifact partitioning.",
            "Run progress uses versioned events; remote snapshot dashboard replacement remains application work.",
        ],
        "references": [
            "https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html",
            "https://docs.python.org/3/library/codecs.html",
        ],
    }
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
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
