"""An ACP model must produce supported outcomes; deterministic results cannot mask errors."""

import copy
import unittest

from dios_reference.app import result
from dios_reference.harness import model_result, prompt
from dios_sdk import WorkloadError
from test_reference_workflows import fixture, response


class HarnessContract(unittest.TestCase):
    def test_three_paths_preserve_model_outcome_and_input_identity(self):
        for scenario in ("sales", "team", "education"):
            value, sources = fixture(scenario)
            proposal = {
                "sources": sources,
                "outcome": result(value, response(sources), "核验")["outcome"],
            }
            actual = model_result(value, proposal, "核验")
            self.assertIs(actual["outcome"], proposal["outcome"])
            self.assertEqual(actual["input_digest"], value["sha256"])
            self.assertEqual(actual["review_status"], "pending_review")
            self.assertIn("mcp__dios__knowledge_search", prompt(value, "核验"))

    def test_wrong_recommendation_is_rejected_instead_of_repaired(self):
        value, sources = fixture("sales")
        outcome = result(value, response(sources), "核验")["outcome"]
        outcome["recommended"], outcome["excluded"] = (
            outcome["excluded"],
            outcome["recommended"],
        )
        with self.assertRaisesRegex(WorkloadError, "EVIDENCE_INSUFFICIENT"):
            model_result(value, {"sources": sources, "outcome": outcome}, "核验")

    def test_fabricated_owner_or_missing_source_is_rejected(self):
        value, sources = fixture("team")
        proposal = {
            "sources": sources,
            "outcome": result(value, response(sources), "核验")["outcome"],
        }
        fabricated = copy.deepcopy(proposal)
        fabricated["outcome"]["plans"][1]["owner"] = "invented"
        with self.assertRaises(WorkloadError):
            model_result(value, fabricated, "核验")
        proposal["sources"]["documents"].pop()
        with self.assertRaises(WorkloadError):
            model_result(value, proposal, "核验")

    def test_altered_source_and_extra_decision_fields_are_rejected(self):
        value, sources = fixture("education")
        proposal = {
            "sources": sources,
            "outcome": result(value, response(sources), "核验")["outcome"],
        }
        with self.assertRaises(WorkloadError):
            model_result(value, dict(proposal, review_status="approved"), "核验")
        proposal["sources"]["documents"][0]["text"] = "restricted answer"
        with self.assertRaises(WorkloadError):
            model_result(value, proposal, "核验")
