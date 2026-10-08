"""Read-only evidence audit, separate from runner pass flags and native verifier."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def check(directory, *, expected_names=None, backend="sqlite"):
    failures, expected_incomplete, records = [], [], []
    summary = load(directory / "summary.json")
    if expected_names is None:
        assert summary["case_count"] == 15
    else:
        assert summary["case_count"] == len(expected_names)
        assert {x["case"] for x in summary["cases"]} == set(expected_names)
    for item in summary["cases"]:
        name = item["case"]
        row = load(directory / (name + ".json"))
        if "snapshot" not in row:
            failures.append(name + ":missing_snapshot")
            continue
        snapshot = row["snapshot"]
        folder = directory / name
        if backend == "postgresql":
            export = load(folder / "postgres-export.json")
            assert export["backend"] == "postgresql"
            persisted_counts = {key: len(values) for key, values in export["tables"].items()}
            db_events = [(e["id"], json.dumps(e["audit_chain"])) for e in export["tables"]["events"]]
        elif backend == "sqlite":
            connection = sqlite3.connect((folder / "state.db").as_uri() + "?mode=ro", uri=True)
            try:
                persisted_counts = {key: connection.execute("select count(*) from " + table).fetchone()[0]
                    for key, table in {"events": "event_logs", "tasks": "a2a_tasks", "grants": "e2ag_tool_grants",
                                       "approvals": "e2ag_approvals", "invocations": "e2ag_tool_invocations",
                                       "receipts": "hetero_receipts", "conflicts": "hetero_conflicts"}.items()}
                db_events = connection.execute("select id, audit_chain from event_logs").fetchall()
            finally:
                connection.close()
        else:
            raise ValueError("Unsupported audit backend")
        problems = []
        for key, count in persisted_counts.items():
            if count != len(snapshot[key]): problems.append("db_count:" + key)
        actual_tools = lines(folder / "tool-receipts.jsonl")
        actual_artifacts = lines(folder / "artifacts.jsonl")
        expected_count = 1 if name in {"normal", "approved", "identity_conflict", "before_receive_commit",
            "after_receive_commit", "before_dispatch", "after_dispatch_commit", "after_handoff_commit", "approval_race"} else 0
        if len(actual_tools) != expected_count or len(actual_artifacts) != expected_count:
            problems.append("unexpected_tool_or_artifact_count")
        if actual_tools != row.get("tool_operations", []): problems.append("tool_snapshot_diff")
        for artifact in actual_artifacts:
            if artifact != {"name": "report.write", "arguments": {"resource": "report/allowed", "report": "GOVERNANCE RECEIPT"}}:
                problems.append("artifact_content")
        for event_id, serialized in db_events:
            chain = json.loads(serialized)
            previous = ""
            event = next(e for e in snapshot["events"] if e["id"] == event_id)
            if chain != event["audit_chain"]: problems.append("chain_snapshot_diff")
            for index, original in enumerate(chain):
                entry = dict(original)
                stored = entry.pop("entry_hash")
                raw = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                if (hashlib.sha256(raw).hexdigest() != stored or entry["previous_hash"] != previous
                    or entry["sequence"] != index or entry["trace_id"] != event["trace_id"]):
                    problems.append("audit_hash_or_order")
                previous = stored
            for task in snapshot["tasks"]:
                if task["context_id"] != event_id or task["trace_id"] != event["trace_id"]:
                    problems.append("task_binding")
                linked = [e for e in chain if e["stage"] == "a2a_task" and e["evidence"].get("task_id") == task["id"]]
                if len(linked) != 1:
                    if name == "during_task_launch":
                        expected_incomplete.append(name + ":task_creation_interrupted_before_audit_link")
                    else:
                        problems.append("missing_or_duplicate_task_evidence")
            for invocation in snapshot["invocations"]:
                if invocation["event_log_id"] != event_id or invocation["trace_id"] != event["trace_id"]:
                    problems.append("invocation_binding")
                grant = next((g for g in snapshot["grants"] if g["id"] == invocation["grant_id"]), None)
                if grant is None or grant["task_id"] != invocation["task_id"]:
                    problems.append("invocation_grant_binding")
                if invocation["method"] == "tools/call" and invocation["state"] != "response_received":
                    problems.append("unexpected_unknown_tool_call")
        if name == "during_task_launch":
            if row["first"]["acknowledge"] or row["repeat"]["acknowledge"] or row["business_complete"]:
                problems.append("uncertain_state_reported_complete")
        if not row.get("passed"): problems.append("runner_failure")
        failures += [name + ":" + p for p in problems]
        records.append({"case": name, "problems": problems, "tool_receipts": len(actual_tools),
                        "persisted_counts": persisted_counts})
    return {"status": "passed" if not failures else "failed", "failures": failures,
            "expected_incomplete": expected_incomplete, "cases": records,
            "scope": "independently read " + backend + " counts/audit chain and tool receipt/artifact files; not external human review"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists(): raise SystemExit("Refusing overwrite")
    result = check(args.directory.resolve())
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "failures": result["failures"],
                      "expected_incomplete": result["expected_incomplete"]}))
    raise SystemExit(0 if result["status"] == "passed" else 1)
