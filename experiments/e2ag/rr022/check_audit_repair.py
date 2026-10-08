"""Run backend regression and verify fresh state-pilot evidence after audit repair."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, HERE, file_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a new filename")
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    assert before["cases_sha256"] == after["cases_sha256"]
    assert before["harness_sha256"] == after["harness_sha256"]
    assert after["passed"] == after["total"] == 9
    pairs = []
    for old, new in zip(before["rows"], after["rows"], strict=True):
        assert (old["case"], old["method"], old["job"]) == (new["case"], new["method"], new["job"])
        a, b = old["observed"], new["observed"]
        assert a["decision_sequence"] == b["decision_sequence"]
        assert a["tool_receipts"] == b["tool_receipts"]
        if new["case"].startswith("L"):
            assert a["response"] == b["response"]
            call_entries = [e for e in b["audit_added_by_call"] if e["stage"] == "tool_call"]
            assert len(call_entries) == 1 and call_entries[0]["outcome"] == "deny"
            assert call_entries[0]["evidence"]["reason"] == b["response"]["body"]["error"]["data"]["reason"]
            assert b["audit_verification"] is True and b["grant_call_count"] == 1
            pairs.append({"case": new["case"], "response_unchanged": True, "receipts": len(b["tool_receipts"]),
                          "before_call_entries": len([e for e in a["audit_added_by_call"] if e["stage"] == "tool_call"]),
                          "after_call_entries": len(call_entries), "reason": call_entries[0]["evidence"]["reason"]})
    command = [str(ROOT / "backend/.venv/Scripts/python.exe"), "-m", "unittest", "discover", "-s", "tests", "-p", "test_e2ag*.py", "-v"]
    proc = subprocess.run(command, cwd=ROOT / "backend", text=True, encoding="utf-8", capture_output=True, timeout=240)
    report = {"kind": "audit repair regression, not new independent experimental samples",
              "before_sha256": file_hash(args.before), "after_sha256": file_hash(args.after),
              "checker_sha256": file_hash(Path(__file__)), "lifecycle_pairs": pairs,
              "unittest": {"command": command, "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
              "source_sha256": {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p) for p in [
                  ROOT / "backend/app/services/e2ag_tool_gateway.py", ROOT / "backend/app/api/internal/e2ag_mcp.py",
                  ROOT / "backend/tests/test_e2ag_tool_gateway.py"]},
              "pass": proc.returncode == 0,
              "scope": "POST requests with parsed object payload and authenticated grant/event association; unauthenticated, malformed, GET/DELETE and concurrent audit append coverage not added."}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(proc.stderr[-4500:])
    print(json.dumps({"lifecycle_pairs": pairs, "pass": report["pass"], "output": str(args.output)}, ensure_ascii=False, indent=2))
    if not report["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    main()
