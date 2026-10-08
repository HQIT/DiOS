"""Independent read-only audit of AMQP qualification artifacts and databases."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

MODES = ["normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"]
CUTS = ["before_receive_commit", "after_receive_commit", "after_handoff_commit"]
INVALID = {"bad_json": "INVALID_JSON", "reserved_source": "INVALID_ENVELOPE_FIELDS",
           "message_id_mismatch": "MESSAGE_ID_ENVELOPE_MISMATCH"}
NAMES = set(MODES + CUTS + ["pending_redelivery", "approved_redelivery", "identity_conflict",
                          "distinct_identity", "nested_source"] + list(INVALID))
TABLES = {"events": "event_logs", "tasks": "a2a_tasks", "grants": "e2ag_tool_grants",
          "approvals": "e2ag_approvals", "invocations": "e2ag_tool_invocations",
          "receipts": "hetero_receipts", "conflicts": "hetero_conflicts"}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def check(directory):
    errors, records = [], []
    summary = load(directory / "summary.json")
    if {r["case"] for r in summary["cases"]} != NAMES or summary["case_count"] != len(NAMES):
        errors.append("case_manifest_mismatch")
    acl = load(directory / "acl-controls.json")["records"]
    if {r["case"] for r in acl} != {"wrong_password", "foreign_queue", "foreign_exchange"}:
        errors.append("acl_manifest")
    for entry in acl:
        if not entry["denied"] or (entry["case"] != "wrong_password" and entry.get("reply_code") != 403):
            errors.append("acl:" + entry["case"])
    info = load(directory / "broker.json")
    if any(p["HostIp"] != "127.0.0.1" for ports in info["ports"].values() if ports for p in ports):
        errors.append("non_loopback_broker")
    for name in sorted(NAMES):
        problems = []
        row = load(directory / (name + ".json"))
        folder = directory / name
        if "snapshot" not in row:
            errors.append(name + ":missing_snapshot")
            continue
        connection = sqlite3.connect((folder / "state.db").as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {key: [dict(r) for r in connection.execute("select * from " + table)]
                      for key, table in TABLES.items()}
        finally:
            connection.close()
        counts = {key: len(values) for key, values in tables.items()}
        for key in TABLES:
            if counts[key] != len(row["snapshot"][key]): problems.append("snapshot_count:" + key)
        rejected = name in INVALID
        event_count = 0 if rejected else 2 if name == "distinct_identity" else 1
        blocked_admission = name in {"deny", "pending", "pending_redelivery"}
        tool_count = (0 if rejected or blocked_admission or name in {"tool", "resource", "revoked", "expired"}
                      else event_count)
        if counts["events"] != event_count: problems.append("event_count")
        if counts["tasks"] != (0 if blocked_admission else event_count): problems.append("task_count")
        if counts["receipts"] != event_count: problems.append("receipt_count")
        if counts["conflicts"] != (1 if name == "identity_conflict" else 0): problems.append("conflict_count")
        operations = lines(folder / "tool-receipts.jsonl")
        artifacts = lines(folder / "artifacts.jsonl")
        if len(operations) != tool_count or len(artifacts) != tool_count: problems.append("upstream_count")
        if operations != row["tool_operations"]: problems.append("upstream_snapshot")
        for artifact in artifacts:
            if artifact != {"name": "report.write", "arguments": {"resource": "report/allowed", "report": "GOVERNANCE RECEIPT"}}:
                problems.append("artifact_value")
        task_by_id = {t["id"]: t for t in tables["tasks"]}
        grant_by_id = {g["id"]: g for g in tables["grants"]}
        for event in tables["events"]:
            ce = json.loads(event["cloud_event"])
            if ce["source"] != "amqp/fixture" or ce["type"] != "amqp.received": problems.append("source_binding")
            if name == "nested_source" and ce["data"].get("source") != "trusted/forged": problems.append("nested_payload_preservation")
            matching = [r for r in tables["receipts"] if r["event_id"] == ce["id"]]
            if len(matching) != 1 or matching[0]["event_log_id"] != event["id"] or matching[0]["phase"] != "recorded":
                problems.append("receipt_event_binding")
            chain = json.loads(event["audit_chain"])
            previous = ""
            for index, original in enumerate(chain):
                entry = dict(original)
                digest = entry.pop("entry_hash")
                actual = hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if digest != actual or entry["previous_hash"] != previous or entry["sequence"] != index or entry["trace_id"] != event["trace_id"]:
                    problems.append("audit_chain")
                previous = digest
            for task in tables["tasks"]:
                if task["context_id"] != event["id"]: continue
                if task["trace_id"] != event["trace_id"]: problems.append("task_trace")
                links = [e for e in chain if e["stage"] == "a2a_task" and e["evidence"].get("task_id") == task["id"]]
                if len(links) != 1: problems.append("task_audit_link")
        for invocation in tables["invocations"]:
            grant = grant_by_id.get(invocation["grant_id"])
            task = task_by_id.get(invocation["task_id"])
            if not grant or not task or grant["task_id"] != task["id"] or invocation["event_log_id"] != task["context_id"] or invocation["trace_id"] != task["trace_id"]:
                problems.append("invocation_binding")
        successful_calls = [i for i in tables["invocations"] if i["method"] == "tools/call" and i["state"] == "response_received"]
        if len(successful_calls) != tool_count: problems.append("forwarded_call_count")
        if name in {"tool", "resource", "revoked", "expired"}:
            if not tables["tasks"] or json.loads(tables["tasks"][0]["artifacts"])[0]["qualification_call_status"] != 403:
                problems.append("missing_pep_denial")
        if name in {"pending", "pending_redelivery", "approved", "approved_redelivery"}:
            status = "pending" if name.startswith("pending") else "approved"
            if counts["approvals"] != 1 or tables["approvals"][0]["status"] != status: problems.append("approval_state")
        processes = lines(folder / "processes.jsonl")
        deliveries = [json.loads(p["stdout"]) for p in processes if p["op"] == "consume" and p["exit_code"] == 0]
        states = lines(folder / "queue-states.jsonl")
        if not states or states[-1]["ready_messages"] != (1 if rejected else 0): problems.append("broker_queue_state")
        if rejected:
            failures = [p for p in processes if p["op"] == "consume"]
            if len(failures) != 2 or any(p["exit_code"] == 0 or INVALID[name] not in p["stderr"] for p in failures):
                problems.append("explicit_bad_input_rejection")
        else:
            if not deliveries or any(not d["broker"]["acknowledged"] for d in deliveries): problems.append("handoff_ack")
            if len(deliveries) < 2 or deliveries[1]["disposition"] != "duplicate": problems.append("repeat_identity")
            if name in CUTS + ["pending_redelivery", "approved_redelivery"]:
                if not deliveries[0]["broker"]["redelivered"]: problems.append("missing_actual_redelivery")
                if not any(p["cut"] and p["exit_code"] in {80, 81, 84} for p in processes): problems.append("missing_process_cut")
        if not row["passed"]: problems.append("runner_failure")
        records.append({"case": name, "counts": counts, "tool_receipts": len(operations), "problems": problems})
        errors.extend(name + ":" + p for p in problems)
    return {"status": "passed" if not errors else "failed", "failures": errors, "cases": records,
            "scope": "independent SQLite/audit/receipt/process/broker observation check; not external human review",
            "retained_invalid_inputs": sorted(INVALID)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists(): raise SystemExit("Refusing overwrite")
    result = check(args.directory.resolve())
    result["checker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "failures": result["failures"]}))
    raise SystemExit(0 if result["status"] == "passed" else 1)
