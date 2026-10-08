"""Check paired inputs, native effect counts and scoped audit observations."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from run_paired_tool_pilot import file_hash, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a new evidence filename")
    report = json.loads(args.result.read_text(encoding="utf-8"))
    assert report["total"] == report["passed"] == 8
    checked = []
    for case in ("T01", "L01", "L02", "CTRL_BEFORE_EXPIRY"):
        rows = {r["method"]: r for r in report["rows"] if r["case"] == case}
        assert set(rows) == {"e2ag", "libos"}
        left, right = rows["e2ag"], rows["libos"]
        a, b = left["observed"], right["observed"]
        assert left["job"] == right["job"]
        assert a["input_sha256"] == b["input_sha256"] == digest(left["job"])
        assert a["decision_sequence"] == b["decision_sequence"]
        assert a["tool_receipts"] == b["tool_receipts"]
        expected = left["expected"]["receipt_count"]
        assert b["provider_calls"]["validate_and_call"] == expected
        assert a["audit_verification"] is True
        assert b["audit_after_call_count"] - b["audit_before_call_count"] == len(b["audit_added_by_call"])
        for item in b["tool_receipts"]:
            assert item["tool"] == right["job"]["input"]["call"]["tool"]
            assert item["arguments"] == right["job"]["input"]["call"]["arguments"]
        for cap in b["capabilities_before_call"]:
            assert cap["subject"] == b["native_process_id"]
            assert datetime.fromisoformat(cap["issued_at"]) <= datetime.fromisoformat(cap["expires_at"])
            assert cap["status"] == ("revoked" if case == "L02" else "active")
        if expected == 0:
            assert b["rejection"]["type"] == "CapabilityDenied"
            assert not b["external_effects"]
        else:
            assert b["native_result"]["ok"] is True
            assert any(e["action"] == "primitive.mcp.call" for e in b["audit_added_by_call"])
        checked.append({"case": case, "decision": a["decision_sequence"], "receipts_each": expected,
                        "e2ag_call_audit_entries": len([e for e in a["audit_added_by_call"] if e["stage"] == "tool_call"]),
                        "libos_new_audit_entries": len(b["audit_added_by_call"]), "pass": True})
    result = {"kind": "paired lifecycle interface validation, not statistical comparison",
              "source_result_sha256": file_hash(args.result), "checker_sha256": file_hash(Path(__file__)),
              "rows": checked, "pass": True,
              "interpretation": "Same execution outcomes. Audit observations are limited to this native MCP entry point and pinned configuration; do not imply libOS lacks audit capability."}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
