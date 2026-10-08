"""Independent persisted-row, wire-record and artifact checks for R0--R5."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

TABLES = {"events": "event_logs", "tasks": "a2a_tasks", "grants": "e2ag_tool_grants", "approvals": "e2ag_approvals",
          "invocations": "e2ag_tool_invocations", "receipts": "hetero_receipts", "conflicts": "hetero_conflicts"}
JSON_FIELDS = {"cloud_event", "audit_chain", "artifacts", "matched_agent_ids", "allowed_tools", "argument_constraints", "response"}
REASONS = {"tool": "MCP_TOOL_NOT_GRANTED", "resource": "MCP_ARGUMENT_ENUM_DENIED", "revoked": "TOOL_GRANT_NOT_ACTIVE",
           "revoked_reconnect": "TOOL_GRANT_NOT_ACTIVE", "expired": "TOOL_GRANT_EXPIRED"}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def tables(folder, backend):
    if backend == "postgresql": return load(folder / "postgres-export.json")["tables"]
    db = sqlite3.connect((folder / "state.db").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        result = {}
        for key, table in TABLES.items():
            result[key] = [{k: json.loads(v) if k in JSON_FIELDS and isinstance(v, str) else v
                            for k, v in dict(r).items() if k != "token_hash"} for r in db.execute("select * from " + table)]
        return result
    finally:
        db.close()


def audit_case(folder, mode, ingress, transport, backend):
    raw = tables(folder, backend)
    problems = []
    def need(condition, label):
        if not condition: problems.append(label)
    allowed = mode in {"normal", "approved"}
    no_task = mode in {"deny", "pending"}
    need(len(raw["events"]) == 1, "one_event")
    need(len(raw["tasks"]) == (0 if no_task else 1), "task_count")
    need(len(raw["grants"]) == (0 if no_task else 1), "grant_count")
    need(len(raw["receipts"]) == 1 and raw["receipts"][0]["phase"] == "recorded", "durable_handoff")
    need(not raw["conflicts"], "unexpected_identity_conflict")
    tool_rows, artifacts = lines(folder / "tool-receipts.jsonl"), lines(folder / "artifacts.jsonl")
    need(len(tool_rows) == int(allowed) and len(artifacts) == int(allowed), "tool_artifact_count")
    expected_artifact = {"name": "report.write", "arguments": {"resource": "report/allowed", "report": "GOVERNANCE RECEIPT"}}
    need(all(r == expected_artifact for r in artifacts), "artifact_content")
    need(all(r == {"method": "tools/call", **expected_artifact} for r in tool_rows), "tool_content")
    if len(raw["events"]) != 1: return problems, raw
    event = raw["events"][0]
    cloud = event["cloud_event"]
    data = {"text": "governance receipt", "environment": "production",
            "requested_action": "secret.read" if mode in {"pending", "approved"} else "report.create"}
    need(cloud["source"] == ingress + "/fixture" and cloud["type"] == ingress + ".received", "source_type_binding")
    need(cloud["data"] == data and cloud["subject"] == "delivery-1" and cloud["id"] == "delivery-1", "normalization")
    canonical = {"id": "delivery-1", "subject": "Qualification task", "data": data}
    digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    need(cloud["deliverydigest"] == digest and raw["receipts"][0]["digest"] == digest, "payload_digest")
    need(raw["receipts"][0]["event_log_id"] == event["id"], "receipt_event_link")
    expected_status = "denied" if mode == "deny" else "approval_required" if mode == "pending" else "dispatched" if allowed else "failed"
    need(event["status"] == expected_status, "event_status")
    chain, previous = event["audit_chain"], ""
    for index, original in enumerate(chain):
        entry = dict(original)
        stored = entry.pop("entry_hash")
        actual = hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        need(stored == actual and entry["previous_hash"] == previous and entry["sequence"] == index and entry["trace_id"] == event["trace_id"], "audit_hash_link")
        previous = stored
    for task in raw["tasks"]:
        need(task["context_id"] == event["id"] and task["trace_id"] == event["trace_id"], "task_event_binding")
        need(len([e for e in chain if e["stage"] == "a2a_task" and e["evidence"].get("task_id") == task["id"]]) == 1, "task_evidence")
        need(task["status"] == ("completed" if allowed else "failed"), "task_status")
        if mode in REASONS:
            artifact = task["artifacts"][0]
            need(artifact["qualification_call_status"] == 403 and artifact["response"]["error"]["data"]["reason"] == REASONS[mode], "explicit_denial")
            need(any(e["stage"] == "tool_call" and e["outcome"] == "deny" and e["evidence"].get("reason") == REASONS[mode] for e in chain), "denial_evidence")
    for invocation in raw["invocations"]:
        need(invocation["event_log_id"] == event["id"] and invocation["trace_id"] == event["trace_id"], "invocation_event_binding")
        need(any(g["id"] == invocation["grant_id"] and g["task_id"] == invocation["task_id"] for g in raw["grants"]), "invocation_grant_binding")
        need(invocation["state"] == "response_received", "unexpected_unknown")
        for stage in ["tool_intent", "tool_call"]:
            need(len([e for e in chain if e["stage"] == stage and e["evidence"].get("invocation_id") == invocation["id"]]) == 1, stage + "_evidence")
    for grant in raw["grants"]:
        need(grant["event_log_id"] == event["id"] and grant["trace_id"] == event["trace_id"]
             and any(t["id"] == grant["task_id"] and t["agent_id"] == grant["agent_id"] for t in raw["tasks"]), "grant_binding")
        need(grant["allowed_tools"] == ["report.write"] and grant["argument_constraints"]["tools"]["report.write"]["resource"]["enum"] == ["report/allowed"], "grant_scope_snapshot")
        need(len([e for e in chain if e["stage"] == "tool_grant" and e["outcome"] == "grant_issued" and e["evidence"].get("grant_id") == grant["id"]]) == 1, "grant_evidence")
    need(len(raw["invocations"]) == (0 if no_task else 3 if allowed else 2), "forwarded_call_count")
    if mode in {"pending", "approved"}:
        need(len(raw["approvals"]) == 1 and raw["approvals"][0]["status"] == mode, "approval_terminal")
        need(all(a["event_log_id"] == event["id"] and a["trace_id"] == event["trace_id"] for a in raw["approvals"]), "approval_binding")
    else:
        need(not raw["approvals"], "unexpected_approval")
    if ingress == "http":
        wire = lines(folder / "http-client.jsonl")
        inputs = lines(folder / "http-inputs.jsonl")
        need(len(wire) == 2 and all(r.get("status") == 202 for r in wire), "actual_http_handoffs")
        need(len(inputs) == 2 and all(r["signature_verified"] and r["normalized"] == cloud for r in inputs), "http_mapping_evidence")
        need(wire[-1].get("response", {}).get("disposition") == "duplicate", "http_duplicate")
    if ingress == "amqp":
        need(lines(folder / "queue-states.jsonl")[-1]["ready_messages"] == 0, "amqp_queue_drained")
        processes = lines(folder / "processes.jsonl")
        replies = [json.loads(p["stdout"]) for p in processes if p["op"] == "consume" and p["exit_code"] == 0]
        need(len(replies) == 2 and all(r["broker"]["acknowledged"] for r in replies), "amqp_ack_after_handoff")
        need(replies[-1]["disposition"] == "duplicate", "amqp_duplicate")
        if folder.name == "J01":
            need(replies[0]["broker"]["redelivered"] and any(p["exit_code"] == 84 for p in processes), "amqp_recovery_before_approval")
    if transport == "stdio":
        frames = lines(folder / "stdio-messages.jsonl")
        lifecycle = lines(folder / "stdio-processes.jsonl")
        expected = [] if no_task else ["initialize", "notifications/initialized"] + (["tools/call"] if allowed else [])
        need([r["payload"]["method"] for r in frames] == expected, "stdio_wire_methods")
        need([r["phase"] for r in lifecycle] == ([] if no_task else ["started", "exited"]), "stdio_child_lifecycle")
        need(len({r["pid"] for r in frames}) == (0 if no_task else 1), "stdio_child_identity")
    if mode == "revoked_reconnect":
        reconnect = lines(folder / "reconnect.jsonl")
        need(len(reconnect) == 1 and reconnect[0]["revocation_committed"] and reconnect[0]["old_session_closed"]
             and reconnect[0]["reinitialize_status"] == 403
             and reconnect[0]["reinitialize_response"]["error"]["data"]["reason"] == "TOOL_GRANT_NOT_ACTIVE", "reconnect_boundary")
    return problems, raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.directory.resolve()
    freeze = load(root / "freeze.json")
    results, failures = [], []
    for case in freeze["cases"]:
        config, name, mode = case["config"], case["case"], case["mode"]
        try:
            problems, raw = audit_case(root / config / name, mode, *freeze["configs"][config])
        except Exception as exc:
            problems, raw = [type(exc).__name__ + ":" + str(exc)], {}
        failures.extend(config + "/" + name + ":" + p for p in problems)
        results.append({**case, "problems": problems, "counts": {k: len(v) for k, v in raw.items()}})
    result = {"status": "failed" if failures else "passed", "failures": failures, "cases": results,
        "scope": "independent persisted rows, actual transport records, tool artifacts and audit links; deterministic inputs, not human review",
        "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "case_count": len(results), "failures": failures}))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
