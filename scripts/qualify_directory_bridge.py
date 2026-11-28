"""Bounded local qualification; uses installed application wheel and isolated fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests"),
                   str(ROOT / "applications/directory-bridge/tests")]
    import dios_directory.mapping as installed
    assert not Path(installed.__file__).resolve().is_relative_to(ROOT / "applications"), "Install the wheel first"
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(["test_mapping", "test_sync", "test_runtime_integration", "test_principal_updates"])
    cases = []

    class Result(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test); cases.append({"test": test.id(), "state": "passed"})
        def addFailure(self, test, err):
            super().addFailure(test, err); cases.append({"test": test.id(), "state": "failed"})
        def addError(self, test, err):
            super().addError(test, err); cases.append({"test": test.id(), "state": "error"})
        def addSkip(self, test, reason):
            super().addSkip(test, reason); cases.append({"test": test.id(), "state": "skipped", "reason": reason})

    start = time.time()
    with (output / "tests.log").open("w") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Result).run(suite)
    sources = ["applications/directory-bridge/src/dios_directory/mapping.py",
               "applications/directory-bridge/tests/test_mapping.py",
               "applications/directory-bridge/tests/test_runtime_integration.py",
               "applications/directory-bridge/tests/test_sync.py",
               "applications/directory-bridge/src/dios_directory/scim.py",
               "applications/directory-bridge/src/dios_directory/journal.py",
               "applications/directory-bridge/src/dios_directory/sync.py",
               "backend/app/runtime/principal_updates.py", "backend/app/runtime/cli.py",
               "scripts/qualify_directory_bridge.py"]
    report = {"result": "PASS" if result.wasSuccessful() and not result.skipped else "FAIL",
              "observed_at": start, "duration_seconds": round(time.time()-start, 3),
              "tests_run": result.testsRun, "cases": cases,
              "application": "dios-directory-bridge==0.2.0",
              "installed_module_sha256": hashlib.sha256(Path(installed.__file__).read_bytes()).hexdigest(),
              "source_hashes": {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in sources},
              "conditions": {"database": "isolated temporary SQLite", "identity": "synthetic independent principals",
                             "worker_adapter": "deterministic test adapter; no external model",
                             "administration": "real subprocess identity-policy/identity-update CLI",
                             "directory": "loopback synthetic SCIM HTTP service, conditional GET and configured resource IDs",
                             "synchronization": "independent sync CLI, durable outbox/checkpoint, inherited process lock",
                             "authorization": "real API middleware and E2AG admission/execution/result verifier"},
              "not_qualified": ["live enterprise SCIM source", "automatic directory discovery and identity provisioning", "enterprise SSO login",
                                "atomic multi-principal directory publication", "physical-node failure recovery"]}
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"result": report["result"], "tests_run": result.testsRun, "report": str(output/"result.json")}))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__": raise SystemExit(main())
