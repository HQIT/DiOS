"""Validate state pilot evidence, without conflating control and audit coverage."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from run_state_pilot import HERE, file_hash, digest, run_worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a new evidence file")
    report = json.loads(args.result.read_text(encoding="utf-8"))
    assert report["passed"] == report["total"] == 9
    approvals = [r for r in report["rows"] if r["case"].startswith("A")]
    # Human answer is the ONLY changed item among these six worker inputs.
    assert len({digest(r["job"]) for r in approvals}) == 1
    traces = []
    for row in report["rows"]:
        observed = row["observed"]
        assert observed["input_sha256"] == digest(row["job"])
        assert "answer" not in row["job"]["input"]["approval"]
        for item in observed["tool_receipts"]:
            assert item["tool"] == row["job"]["input"]["call"]["tool"]
            assert item["arguments"] == row["job"]["input"]["call"]["arguments"]
        if row in approvals:
            messages = [m["type"] for m in row["pipe_trace"]]
            assert messages[0] == "approval_requested"
            if row["harness_answer"] is None:
                assert messages == ["approval_requested"]
                assert observed["pending_observed"]
            else:
                assert messages == ["approval_requested", "harness_answer_delivered", "result"]
                if row["method"] == "e2ag":
                    assert observed["audit_verification"] is True
                    assert observed["created_tasks"] == (1 if row["harness_answer"] == "yes" else 0)
                    duplicate = observed["duplicate_reply"]
                    assert duplicate["error"] == "APPROVAL_ALREADY_DECIDED"
                    assert duplicate["tasks_before"] == duplicate["tasks_after"]
            traces.append({"case": row["case"], "method": row["method"], "message_order": messages, "pass": True})
    expected_reasons = {"L01": "TOOL_GRANT_EXPIRED", "L02": "TOOL_GRANT_NOT_ACTIVE", "L03": "TOOL_GRANT_TASK_MISMATCH"}
    audit = []
    for row in report["rows"]:
        if row["case"] not in expected_reasons:
            continue
        observed = row["observed"]
        assert observed["audit_verification"] is True
        reason = observed["response"]["body"]["error"]["data"]["reason"]
        assert reason == expected_reasons[row["case"]]
        audit.append({"case": row["case"], "reason": reason, "native_chain_valid": True,
                      "new_audit_entries": len(observed["audit_added_by_call"]),
                      "dedicated_tool_call_entry": any(e["stage"] == "tool_call" for e in observed["audit_added_by_call"])})
    # A normal grant just before expiry must not be rejected by a broken fixture.
    spec = json.loads((HERE / "common-cases-v0.1.json").read_text(encoding="utf-8"))
    data = copy.deepcopy(spec["base_input"])
    data["approval"].pop("answer")
    data["grant"]["expires_at"] = data["host"]["now"] + 1
    job = {"policy": spec["policy"], "input": data}
    observed, trace, stderr = run_worker("e2ag", "lifecycle", job, None)
    passed = (observed.get("error") is None and observed.get("decision_sequence") == ["allow"]
              and len(observed.get("tool_receipts", [])) == 1 and observed.get("audit_verification") is True)
    result = {"kind": "state pilot evidence validation; control checks are not independent workloads",
              "original_result_sha256": file_hash(args.result), "checker_sha256": file_hash(Path(__file__)),
              "state_harness_sha256": file_hash(HERE / "run_state_pilot.py"),
              "approval_input_and_order_checks": traces, "lifecycle_audit_coverage": audit,
              "pre_expiry_positive_control": {"job": job, "observed": observed, "pipe_trace": trace, "stderr": stderr, "pass": passed},
              "execution_checks_pass": passed,
              "audit_followup": "L02/L03 have no new rejection audit entry; L01 records grant_expired but no dedicated tool_call entry. Do not equate valid chain with complete evidence."}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"approval_order_checks": len(traces), "pre_expiry_control": passed, "audit_coverage": audit}, ensure_ascii=False, indent=2))
    assert passed


if __name__ == "__main__":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    main()
