"""Independent raw-SQL export and tool evidence audit, beyond runner flags."""
import argparse
import hashlib
import json
from pathlib import Path
from check_file_recovery import check, load

NAMES = {"normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired",
         "revoked_fresh", "approval_race", "identity_conflict", "before_receive_commit", "after_receive_commit",
         "before_dispatch", "after_dispatch_commit", "after_handoff_commit", "during_task_launch"}


def audit(directory):
    result = check(directory, expected_names=NAMES, backend="postgresql")
    for name in sorted(NAMES):
        raw = load(directory / name / "postgres-export.json")
        tables = raw["tables"]
        chain = tables["events"][0]["audit_chain"]
        types = [r["data_type"] for r in raw["column_types"] if r["table_name"] == "event_logs" and r["column_name"] == "audit_chain"]
        if types != ["jsonb"]: result["failures"].append(name + ":wrong_audit_column_type")
        for invocation in tables["invocations"]:
            intents = [e for e in chain if e["stage"] == "tool_intent" and e["evidence"].get("invocation_id") == invocation["id"]]
            replies = [e for e in chain if e["stage"] == "tool_call" and e["evidence"].get("invocation_id") == invocation["id"]]
            if len(intents) != 1 or (invocation["state"] == "response_received" and len(replies) != 1):
                result["failures"].append(name + ":missing_intent_or_response_link:" + invocation["id"])
        if name == "approval_race":
            row = load(directory / (name + ".json"))["approval_race"]
            if row["synchronized_claims"] != 2 or row["outcomes"].count({"approval": "approved"}) != 1:
                result["failures"].append("approval_race:claim_count")
            if len(tables["approvals"]) != 1 or tables["approvals"][0]["status"] != "approved" or len(tables["tasks"]) != 1:
                result["failures"].append("approval_race:terminal_state")
        if name == "revoked_fresh":
            reason = tables["tasks"][0]["artifacts"][0]["response"]["error"]["data"]["reason"]
            if reason != "TOOL_GRANT_NOT_ACTIVE": result["failures"].append("revoked_fresh:denial_reason")
            if not any(e["stage"] == "tool_call" and e["evidence"].get("reason") == reason for e in chain):
                result["failures"].append("revoked_fresh:missing_denial_evidence")
    contracts = load(directory / "storage-contracts.json")
    raw = load(directory / "storage_contracts/postgres-export.json")["tables"]
    if sorted(contracts["claim_rowcounts"]) != [0, 1] or raw["receipts"][0]["phase"] != "dispatching":
        result["failures"].append("storage:claim_uniqueness")
    chain = raw["events"][0]["audit_chain"]
    previous = ""
    for i, original in enumerate(chain):
        row = dict(original)
        digest = row.pop("entry_hash")
        actual = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != actual or row["previous_hash"] != previous or row["sequence"] != i:
            result["failures"].append("storage:audit_hash")
        previous = digest
    if len(chain) != 2 or {e["evidence"]["actor"] for e in chain} != {"writer-1", "writer-2"}:
        result["failures"].append("storage:lost_audit_update")
    result["status"] = "passed" if not result["failures"] else "failed"
    result["checker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists(): raise SystemExit("Refusing overwrite")
    result = audit(args.directory.resolve())
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "failures": result["failures"]}))
    raise SystemExit(0 if result["status"] == "passed" else 1)
