"""Save reproducible unit/native-admission checks; never label them E2E."""
from pathlib import Path
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import sys
import unittest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Refusing to overwrite qualification result")
    sys.path.insert(0, str(root / "backend"))
    sys.path.insert(0, str(root / "backend/tests"))
    os.chdir(root / "backend")
    suite = unittest.TestSuite()
    for pattern in ("test_file_ingress_qualification.py", "test_e2ag_entry_contracts.py"):
        suite.addTests(unittest.defaultTestLoader.discover(str(root / "backend/tests"), pattern=pattern))
    capture = io.StringIO()
    result = unittest.TextTestRunner(stream=capture, verbosity=2).run(suite)
    paths = [Path(__file__), root / "experiments/e2ag/heterogeneity/file_ingress.py",
             root / "backend/tests/test_file_ingress_qualification.py",
             root / "backend/tests/test_e2ag_entry_contracts.py",
             root / "backend/tests/test_e2ag_dispatcher.py"]
    paths += sorted((root / "backend/app").rglob("*.py"))
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "file_adapter_qualification_not_end_to_end",
        "scope": "real files, native routing/admission; in-memory SQLite; stubbed task execution",
        "status": "passed" if result.wasSuccessful() else "failed",
        "tests_run": result.testsRun,
        "failures": [{"test": str(t), "traceback": msg} for t, msg in result.failures],
        "errors": [{"test": str(t), "traceback": msg} for t, msg in result.errors],
        "skipped": [{"test": str(t), "reason": reason} for t, reason in result.skipped],
        "python": sys.version,
        "source_sha256": {str(p.relative_to(root)).replace("\\", "/"):
                          hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "test_log": capture.getvalue(),
        "not_validated": ["real Agent/tool execution", "process restart durability",
                          "same-id changed-content conflict rejection", "AMQP", "stdio", "PostgreSQL",
                          "hostile concurrent filesystem mutation"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": record["status"], "tests_run": result.testsRun,
                      "failures": len(result.failures), "errors": len(result.errors),
                      "skipped": len(result.skipped), "output": str(output)}, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
