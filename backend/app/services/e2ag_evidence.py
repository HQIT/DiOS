"""Offline relationship checks over a trusted, consistent E2AG snapshot.

Scope: enforced event admission and durable HTTP POST tools/call records.
This does not authenticate the snapshot, re-evaluate policy/actual arguments,
prove physical execution, or reconstruct historical expiry/revocation times.
No DB writes, network access, or changes to the authorization path.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from app.services.e2ag import verify_audit_chain
from app.services.e2ag_arguments import constraints_digest


def verify_execution_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    unsupported: list[str] = []
    unknown: list[str] = []

    def require(ok: bool, code: str) -> None:
        if not ok and code not in errors:
            errors.append(code)

    def index_rows(name: str) -> dict[str, dict]:
        rows = snapshot.get(name)
        if not isinstance(rows, list):
            require(False, f"MISSING_{name.upper()}")
            return {}
        result = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                require(False, f"INVALID_{name.upper()}_ROW")
                continue
            require(row["id"] not in result, f"DUPLICATE_{name.upper()}_ID")
            result[row["id"]] = row
        return result

    event = snapshot.get("event", {})
    chain = event.get("audit_chain") if isinstance(event, dict) else None
    if not isinstance(chain, list) or not all(isinstance(x, dict) and isinstance(x.get("evidence"), dict) for x in chain):
        return {"valid": False, "hash_valid": False, "errors": ["INVALID_CHAIN_SCHEMA"],
                "unsupported": [], "unknown_invocations": []}
    try:
        hash_valid = verify_audit_chain(chain)
    except (TypeError, ValueError, KeyError):
        hash_valid = False
    require(bool(chain), "EMPTY_CHAIN")
    require(bool(event.get("id")) and bool(event.get("trace_id")), "MISSING_EVENT_IDENTITY")
    require(all(x.get("trace_id") == event.get("trace_id") for x in chain), "EVENT_TRACE_MISMATCH")
    tasks, grants = index_rows("tasks"), index_rows("grants")
    invocations, approvals = index_rows("invocations"), index_rows("approvals")

    def records(stage: str, outcome: str | None = None) -> list[dict]:
        return [x for x in chain if x.get("stage") == stage and (outcome is None or x.get("outcome") == outcome)]

    def unique(stage: str, outcome: str, key: str, value: str) -> dict | None:
        found = [x for x in records(stage, outcome) if x["evidence"].get(key) == value]
        require(len(found) == 1, f"{stage.upper()}_{outcome.upper()}_COUNT:{value}")
        return found[0] if len(found) == 1 else None

    known_stages = {"contract", "policy", "dispatch", "approval", "a2a_task", "tool_grant", "tool_intent", "tool_call"}
    unsupported.extend(sorted({f"STAGE:{x.get('stage')}" for x in chain if x.get("stage") not in known_stages}))
    contract, policy = event.get("contract_decision", {}), event.get("policy_decision", {})
    if not isinstance(contract, dict) or not isinstance(policy, dict):
        contract, policy = {}, {}
        require(False, "INVALID_ADMISSION_RECORD")
    if contract.get("mode") != "enforce" or policy.get("mode") != "enforce":
        unsupported.append("NON_ENFORCE_OR_UNSPECIFIED_MODE")
    for stage, saved in (("contract", contract), ("policy", policy)):
        rows = records(stage)
        require(len(rows) == 1, f"{stage.upper()}_COUNT")
        if len(rows) == 1:
            require(rows[0].get("outcome") == saved.get("decision"), f"{stage.upper()}_DECISION_MISMATCH")
    if len(records("contract")) == len(records("policy")) == 1:
        require(chain.index(records("contract")[0]) < chain.index(records("policy")[0]), "POLICY_BEFORE_CONTRACT")
    decision = policy.get("decision")
    require(decision in {"allow", "deny", "approval_required"}, "INVALID_POLICY_DECISION")
    require(contract.get("decision") in {"allow", "deny"}, "INVALID_CONTRACT_DECISION")
    admission = contract.get("decision") == "allow" and decision == "allow"
    approval_entry = None
    if decision == "approval_required":
        approved = [q for q in approvals.values() if q.get("status") == "approved"]
        if tasks or grants or invocations:
            require(len(approved) == 1, "APPROVAL_REQUIRED_FOR_EXECUTION")
        for q in approved:
            approval_entry = unique("approval", "approved", "approval_id", q["id"])
            pending = unique("approval", "pending", "approval_id", q["id"])
            if pending and approval_entry:
                require(chain.index(pending) < chain.index(approval_entry), "APPROVAL_ORDER")
        admission = contract.get("decision") == "allow" and len(approved) == 1 and approval_entry is not None
    for q in approvals.values():
        require(q.get("event_log_id") == event.get("id") and q.get("trace_id") == event.get("trace_id"), "APPROVAL_BINDING")
        require(q.get("status") in {"pending", "approved", "rejected", "expired"}, "INVALID_APPROVAL_STATE")
    require(len(approvals) <= 1, "MULTIPLE_EVENT_APPROVALS")
    if tasks or grants or invocations:
        require(admission, "EXECUTION_WITHOUT_ADMISSION")

    for task_id, task in tasks.items():
        require(task.get("context_id") == event.get("id") and task.get("trace_id") == event.get("trace_id"), f"TASK_EVENT_BINDING:{task_id}")
        require(task.get("agent_id") in event.get("matched_agent_ids", []), f"TASK_TARGET_BINDING:{task_id}")
        task_entries = [x for x in records("a2a_task") if x["evidence"].get("task_id") == task_id]
        require(bool(task_entries), f"MISSING_TASK_RECORD:{task_id}")
    for entry in records("a2a_task"):
        ev = entry["evidence"]
        task = tasks.get(ev.get("task_id"))
        require(task is not None, "ORPHAN_TASK_RECORD")
        if task:
            require(ev.get("agent_id") == task.get("agent_id"), "TASK_RECORD_AGENT_MISMATCH")

    issued_positions = {}
    for grant_id, grant in grants.items():
        task = tasks.get(grant.get("task_id"))
        require(task is not None, f"GRANT_TASK_MISSING:{grant_id}")
        require(grant.get("event_log_id") == event.get("id") and grant.get("trace_id") == event.get("trace_id"), f"GRANT_EVENT_BINDING:{grant_id}")
        if task:
            require(grant.get("agent_id") == task.get("agent_id"), f"GRANT_AGENT_BINDING:{grant_id}")
        issued = unique("tool_grant", "grant_issued", "grant_id", grant_id)
        if issued:
            issued_positions[grant_id] = chain.index(issued)
            for field in ("task_id", "agent_id", "mcp_server_id", "allowed_tools"):
                require(issued["evidence"].get(field) == grant.get(field), f"GRANT_RECORD_{field.upper()}:{grant_id}")
            k = grant.get("argument_constraints")
            if k is not None:
                require(issued["evidence"].get("argument_constraints_sha256") == constraints_digest(k), f"CONSTRAINT_SNAPSHOT_DIGEST:{grant_id}")
            if approval_entry:
                require(chain.index(approval_entry) < chain.index(issued), "GRANT_BEFORE_APPROVAL")
            policies = records("policy")
            require(len(policies) == 1 and chain.index(policies[0]) < chain.index(issued), "GRANT_BEFORE_POLICY")
    for entry in records("tool_grant", "grant_issued"):
        require(entry["evidence"].get("grant_id") in grants, "ORPHAN_GRANT_RECORD")

    for invocation_id, invocation in invocations.items():
        grant_id = invocation.get("grant_id")
        grant = grants.get(grant_id)
        require(grant is not None, f"INVOCATION_GRANT_MISSING:{invocation_id}")
        require(invocation.get("event_log_id") == event.get("id") and invocation.get("trace_id") == event.get("trace_id"), f"INVOCATION_EVENT_BINDING:{invocation_id}")
        if grant:
            require(invocation.get("task_id") == grant.get("task_id"), f"INVOCATION_TASK_BINDING:{invocation_id}")
            patterns = grant.get("allowed_tools", [])
            require(isinstance(patterns, list) and all(isinstance(x, str) for x in patterns), "INVALID_TOOL_PATTERNS")
            if isinstance(patterns, list) and all(isinstance(x, str) for x in patterns):
                require(any(fnmatchcase(str(invocation.get("tool_name", "")), p) for p in patterns), f"INTENT_TOOL_NOT_GRANTED:{invocation_id}")
        if invocation.get("method") != "tools/call":
            unsupported.append(f"INVOCATION_METHOD:{invocation_id}")
        intent = unique("tool_intent", "outcome_unknown", "invocation_id", invocation_id)
        if intent:
            for audit_field, row_field in (("grant_id", "grant_id"), ("task_id", "task_id"), ("method", "method"), ("tool", "tool_name"), ("request_sha256", "request_sha256")):
                require(intent["evidence"].get(audit_field) == invocation.get(row_field), f"INTENT_{audit_field.upper()}:{invocation_id}")
            require(grant_id in issued_positions and issued_positions[grant_id] < chain.index(intent), f"INTENT_WITHOUT_PRIOR_GRANT:{invocation_id}")
        replies = [x for x in records("tool_call", "allow") if x["evidence"].get("invocation_id") == invocation_id]
        state = invocation.get("state")
        if state == "outcome_unknown":
            unknown.append(invocation_id)
            require(not replies and invocation.get("upstream_status") is None and invocation.get("response_sha256") is None, f"UNKNOWN_WITH_RESPONSE:{invocation_id}")
        elif state == "response_received":
            require(len(replies) == 1 and invocation.get("upstream_status") is not None and bool(invocation.get("response_sha256")), f"RESPONSE_RECORD_MISSING:{invocation_id}")
            if len(replies) == 1 and intent:
                require(chain.index(intent) < chain.index(replies[0]), f"RESPONSE_BEFORE_INTENT:{invocation_id}")
        else:
            require(False, f"INVALID_INVOCATION_STATE:{invocation_id}")

    for entry in records("tool_intent"):
        require(entry["evidence"].get("invocation_id") in invocations, "ORPHAN_INTENT_RECORD")
    for entry in records("tool_call"):
        ev = entry["evidence"]
        grant = grants.get(ev.get("grant_id"))
        require(grant is not None, "CALL_GRANT_MISSING")
        if grant:
            for field in ("task_id", "agent_id", "mcp_server_id"):
                require(ev.get(field) == grant.get(field), f"CALL_{field.upper()}_MISMATCH")
        if entry.get("outcome") == "allow":
            invocation = invocations.get(ev.get("invocation_id"))
            require(invocation is not None, "ALLOW_WITHOUT_INVOCATION")
            if invocation:
                for audit_field, row_field in (("grant_id", "grant_id"), ("task_id", "task_id"), ("method", "method"), ("tool", "tool_name"), ("upstream_status", "upstream_status")):
                    require(ev.get(audit_field) == invocation.get(row_field), f"RESPONSE_{audit_field.upper()}_MISMATCH")
                require(invocation.get("state") == "response_received", "ALLOW_WITHOUT_RECEIVED_RESPONSE")
        elif entry.get("outcome") == "deny":
            require(ev.get("upstream_status") is None and not ev.get("invocation_id"), "DENIAL_WITH_FORWARD_EVIDENCE")
        else:
            unsupported.append("CALL_OUTCOME")

    return {"valid": hash_valid and not errors and not unsupported, "hash_valid": hash_valid,
            "errors": errors, "unsupported": sorted(set(unsupported)), "unknown_invocations": unknown,
            "checked": {"tasks": len(tasks), "grants": len(grants), "invocations": len(invocations)}}
