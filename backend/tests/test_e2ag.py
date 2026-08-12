from __future__ import annotations

import unittest

from app.services.e2ag import append_audit_entry, evaluate_event, verify_audit_chain


def event(source: str = "github/acme/repo", event_type: str = "git.push", data: dict | None = None) -> dict:
    return {
        "specversion": "1.0",
        "id": "evt_test",
        "source": source,
        "type": event_type,
        "subject": "refs/heads/main",
        "data": data or {},
    }


class E2AGDecisionTests(unittest.TestCase):
    def test_declared_source_type_pair_is_allowed(self):
        contract, policy = evaluate_event(event())
        self.assertEqual("allow", contract.decision)
        self.assertEqual("git_webhook", contract.contract_type)
        self.assertEqual("allow", policy.decision)

    def test_declared_type_from_wrong_source_is_denied(self):
        contract, policy = evaluate_event(event("webhook/attacker", "git.push"))
        self.assertEqual("deny", contract.decision)
        self.assertEqual("CONTRACT_SOURCE_TYPE_UNBOUND", contract.reason_codes[0])
        self.assertEqual("deny", policy.decision)

    def test_unknown_event_type_is_denied(self):
        contract, _ = evaluate_event(event(event_type="agent.root"))
        self.assertEqual("deny", contract.decision)

    def test_missing_cloudevent_field_is_denied(self):
        value = event()
        del value["id"]
        contract, _ = evaluate_event(value)
        self.assertEqual(("CONTRACT_REQUIRED_FIELD_MISSING",), contract.reason_codes)

    def test_target_event_allowlist_is_enforced(self):
        targets = {
            "agent-1": {
                "governance": {
                    "allowed_event_sources": ["imap/*"],
                    "allowed_event_types": ["email.*"],
                }
            }
        }
        _, policy = evaluate_event(event(), targets)
        self.assertEqual("deny", policy.decision)
        self.assertEqual("POLICY_TARGET_CAPABILITY_VIOLATION", policy.reason_codes[0])

    def test_target_without_governance_contract_is_denied(self):
        _, policy = evaluate_event(event(), {"legacy-agent": {}})
        self.assertEqual("deny", policy.decision)
        self.assertEqual("governance_missing", policy.evidence["violations"][0]["dimension"])

    def test_target_tool_allowlist_is_enforced(self):
        targets = {"agent-1": {"governance": {"allowed_tools": ["git.read"]}}}
        _, policy = evaluate_event(event(data={"requested_tool": "shell.exec"}), targets)
        self.assertEqual("deny", policy.decision)

    def test_required_action_cannot_be_omitted(self):
        targets = {"agent-1": {"governance": {"require_action_declaration": True}}}
        _, policy = evaluate_event(event(), targets)
        self.assertEqual("deny", policy.decision)
        self.assertEqual("action_missing", policy.evidence["violations"][0]["dimension"])

    def test_target_action_allowlist_is_enforced(self):
        targets = {"agent-1": {"governance": {"allowed_actions": ["git.read"]}}}
        _, policy = evaluate_event(event(data={"requested_action": "admin.grant"}), targets)
        self.assertEqual("deny", policy.decision)

    def test_sensitive_production_action_requires_approval(self):
        data = {
            "requested_action": "filesystem.delete",
            "target_resource": "/production/data",
            "environment": "production",
        }
        _, policy = evaluate_event(event(data=data))
        self.assertEqual("approval_required", policy.decision)
        self.assertEqual("POLICY_PRODUCTION_SENSITIVE_ACTION", policy.reason_codes[0])

    def test_sensitive_non_production_action_is_not_escalated(self):
        data = {"requested_action": "filesystem.delete", "environment": "test"}
        _, policy = evaluate_event(event(data=data))
        self.assertEqual("allow", policy.decision)

    def test_audit_chain_detects_tampering(self):
        chain = append_audit_entry(
            [], trace_id="trace-1", stage="contract", outcome="allow",
            evidence={"contract_type": "git_webhook"},
        )
        chain = append_audit_entry(
            chain, trace_id="trace-1", stage="policy", outcome="deny",
            evidence={"reason": "test"},
        )
        self.assertTrue(verify_audit_chain(chain))
        tampered = [dict(item) for item in chain]
        tampered[0]["outcome"] = "deny"
        self.assertFalse(verify_audit_chain(tampered))


if __name__ == "__main__":
    unittest.main()
