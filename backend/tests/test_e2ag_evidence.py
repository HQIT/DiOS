"""Semantic counterexamples retain valid hashes; no external tool execution."""
import copy
import unittest

from app.services.e2ag import append_audit_entry
from app.services.e2ag_evidence import verify_execution_evidence


def rechain(snapshot):
    entries = snapshot["event"]["audit_chain"]
    chain = []
    for entry in entries:
        chain = append_audit_entry(chain, trace_id=entry["trace_id"], stage=entry["stage"],
                                   outcome=entry["outcome"], evidence=entry["evidence"])
    snapshot["event"]["audit_chain"] = chain
    return snapshot


def fixture(state="response_received", approval=False):
    decision = "approval_required" if approval else "allow"
    event = dict(id="e", trace_id="trace", matched_agent_ids=["a"],
                 contract_decision=dict(mode="enforce", decision="allow"),
                 policy_decision=dict(mode="enforce", decision=decision), audit_chain=[])
    task = dict(id="t", trace_id="trace", context_id="e", agent_id="a")
    grant = dict(id="g", trace_id="trace", event_log_id="e", task_id="t", agent_id="a",
                 mcp_server_id="m", allowed_tools=["report.*"], argument_constraints=None)
    invocation = dict(id="j", trace_id="trace", event_log_id="e", task_id="t", grant_id="g",
                      method="tools/call", tool_name="report.read", request_sha256="a" * 64,
                      state=state, upstream_status=200 if state == "response_received" else None,
                      response_sha256="b" * 64 if state == "response_received" else None)
    snapshot = dict(event=event, tasks=[task], grants=[grant], invocations=[invocation], approvals=[])

    def add(stage, outcome, **evidence):
        event["audit_chain"].append(dict(trace_id="trace", stage=stage, outcome=outcome, evidence=evidence))

    add("contract", "allow")
    add("policy", decision)
    if approval:
        snapshot["approvals"] = [dict(id="q", event_log_id="e", trace_id="trace", status="approved")]
        add("approval", "pending", approval_id="q")
        add("approval", "approved", approval_id="q")
    # Actual dispatch may append task evidence after the runtime issues a grant.
    add("tool_grant", "grant_issued", grant_id="g", task_id="t", agent_id="a", mcp_server_id="m", allowed_tools=["report.*"])
    add("a2a_task", "submitted", task_id="t", agent_id="a")
    add("tool_intent", "outcome_unknown", invocation_id="j", grant_id="g", task_id="t",
        method="tools/call", tool="report.read", request_sha256="a" * 64)
    if state == "response_received":
        add("tool_call", "allow", invocation_id="j", grant_id="g", task_id="t", agent_id="a",
            mcp_server_id="m", method="tools/call", tool="report.read", upstream_status=200)
    return rechain(snapshot)


def semantic_mutations():
    """Named edits and required diagnostic prefix, independent of hash validity."""
    return [
        ("missing_grant", lambda s: s.update(grants=[]), "INVOCATION_GRANT_MISSING"),
        ("missing_task", lambda s: s.update(tasks=[]), "GRANT_TASK_MISSING"),
        ("missing_issuance", lambda s: s["event"]["audit_chain"].pop(2), "TOOL_GRANT_GRANT_ISSUED_COUNT"),
        ("missing_intent", lambda s: s["event"]["audit_chain"].pop(4), "TOOL_INTENT_OUTCOME_UNKNOWN_COUNT"),
        ("wrong_call_task", lambda s: s["event"]["audit_chain"][-1]["evidence"].update(task_id="other"), "CALL_TASK_ID_MISMATCH"),
        ("wrong_call_grant", lambda s: s["event"]["audit_chain"][-1]["evidence"].update(grant_id="other"), "CALL_GRANT_MISSING"),
        ("wrong_task_event", lambda s: s["tasks"][0].update(context_id="other"), "TASK_EVENT_BINDING"),
        ("wrong_grant_agent", lambda s: s["grants"][0].update(agent_id="other"), "GRANT_AGENT_BINDING"),
        ("wrong_invocation_trace", lambda s: s["invocations"][0].update(trace_id="other"), "INVOCATION_EVENT_BINDING"),
        ("wrong_request_digest", lambda s: s["event"]["audit_chain"][4]["evidence"].update(request_sha256="c" * 64), "INTENT_REQUEST_SHA256"),
        ("response_before_intent", lambda s: s["event"]["audit_chain"].insert(4, s["event"]["audit_chain"].pop()), "RESPONSE_BEFORE_INTENT"),
        ("duplicate_intent", lambda s: s["event"]["audit_chain"].append(copy.deepcopy(s["event"]["audit_chain"][4])), "TOOL_INTENT_OUTCOME_UNKNOWN_COUNT"),
        ("wrong_response_status", lambda s: s["event"]["audit_chain"][-1]["evidence"].update(upstream_status=500), "RESPONSE_UPSTREAM_STATUS_MISMATCH"),
        ("missing_response", lambda s: s["event"]["audit_chain"].pop(), "RESPONSE_RECORD_MISSING"),
        ("false_unknown", lambda s: s["invocations"][0].update(state="outcome_unknown"), "UNKNOWN_WITH_RESPONSE"),
        ("missing_policy", lambda s: s["event"]["audit_chain"].pop(1), "POLICY_COUNT"),
        ("policy_before_contract", lambda s: s["event"]["audit_chain"].insert(0, s["event"]["audit_chain"].pop(1)), "POLICY_BEFORE_CONTRACT"),
        ("unselected_agent", lambda s: s["event"].update(matched_agent_ids=["other"]), "TASK_TARGET_BINDING"),
        ("orphan_response", lambda s: s["event"]["audit_chain"][-1]["evidence"].update(invocation_id="other"), "ALLOW_WITHOUT_INVOCATION"),
    ]


class EvidenceTests(unittest.TestCase):
    def test_hash_valid_semantic_counterexamples(self):
        for name, edit, code in semantic_mutations():
            with self.subTest(name=name):
                s = fixture()
                edit(s)
                result = verify_execution_evidence(rechain(s))
                self.assertTrue(result["hash_valid"], result)
                self.assertFalse(result["valid"], result)
                self.assertTrue(any(e.startswith(code) for e in result["errors"]), result)

    def test_normal_response_and_unknown(self):
        for state in ("response_received", "outcome_unknown"):
            with self.subTest(state=state):
                result = verify_execution_evidence(fixture(state))
                self.assertTrue(result["valid"], result)
                self.assertEqual(["j"] if state == "outcome_unknown" else [], result["unknown_invocations"])

    def test_approved_event_and_missing_approval(self):
        s = fixture(approval=True)
        self.assertTrue(verify_execution_evidence(s)["valid"])
        s["approvals"] = []
        result = verify_execution_evidence(s)
        self.assertFalse(result["valid"])
        self.assertIn("APPROVAL_REQUIRED_FOR_EXECUTION", result["errors"])

    def test_denied_event_cannot_have_a_task(self):
        s = fixture()
        s["event"]["policy_decision"]["decision"] = "deny"
        s["event"]["audit_chain"][1]["outcome"] = "deny"
        result = verify_execution_evidence(rechain(s))
        self.assertTrue(result["hash_valid"])
        self.assertIn("EXECUTION_WITHOUT_ADMISSION", result["errors"])

    def test_early_denial_without_downstream_objects(self):
        s = fixture()
        s.update(tasks=[], grants=[], invocations=[])
        s["event"]["policy_decision"]["decision"] = "deny"
        s["event"]["audit_chain"] = s["event"]["audit_chain"][:2]
        s["event"]["audit_chain"][1]["outcome"] = "deny"
        self.assertTrue(verify_execution_evidence(rechain(s))["valid"])

    def test_legacy_allow_without_durable_intent_is_not_accepted(self):
        s = fixture()
        s["invocations"] = []
        s["event"]["audit_chain"].pop(4)
        s["event"]["audit_chain"][-1]["evidence"].pop("invocation_id")
        self.assertFalse(verify_execution_evidence(rechain(s))["valid"])

    def test_hash_tamper_is_separate_from_semantic_errors(self):
        s = fixture()
        s["event"]["audit_chain"][-1]["entry_hash"] = "f" * 64
        result = verify_execution_evidence(s)
        self.assertFalse(result["hash_valid"])
        self.assertFalse(result["valid"])
        self.assertEqual([], result["errors"])

    def test_unsupported_mode_and_control_method(self):
        s = fixture()
        s["event"]["policy_decision"]["mode"] = "off"
        self.assertFalse(verify_execution_evidence(s)["valid"])
        self.assertTrue(verify_execution_evidence(s)["unsupported"])
        s = fixture()
        s["invocations"][0]["method"] = "tools/list"
        self.assertTrue(verify_execution_evidence(s)["unsupported"])

    def test_empty_chain_not_vacuously_complete(self):
        s = fixture()
        s["event"]["audit_chain"] = []
        self.assertIn("EMPTY_CHAIN", verify_execution_evidence(s)["errors"])

    def test_does_not_mutate_evidence(self):
        s = fixture()
        saved = copy.deepcopy(s)
        verify_execution_evidence(s)
        self.assertEqual(saved, s)


if __name__ == "__main__":
    unittest.main()
