"""A green bounded CI gate requires all selected native assertions, never skips."""

import importlib.util
import unittest
from copy import deepcopy
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "scripts/qualify_delivery.py"
spec = importlib.util.spec_from_file_location("delivery_gate", SOURCE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class DeliveryGateTests(unittest.TestCase):
    def test_incomplete_suite_is_not_confused_with_failed_selected_checks(self):
        report = {
            "components_passed": True,
            "sources_unchanged": True,
            "selected_cases": ["R13"],
            "cases": [{"id": "R13", "status": "passed", "gaps": []}],
            "outcomes": {"native": "passed"},
            "complete": False,
            "observations": [
                {
                    "case": "R13",
                    "actual_concurrency_before_drain": 3,
                    "prompt_calls": 4,
                    "failed_image_rolled_back": True,
                    "e2ag": "enforced",
                }
            ],
        }
        self.assertTrue(gate.accepted(report))
        for mutation in (
            {"components_passed": False},
            {"sources_unchanged": False},
            {"selected_cases": ["R14"]},
            {"observations": []},
            {"outcomes": {}},
            {"outcomes": {"native": "skipped"}},
            {"outcomes": {"native": "failed"}},
            {"cases": [{"id": "R13", "status": "passed", "gaps": ["missing"]}]},
        ):
            with self.subTest(mutation=mutation):
                self.assertFalse(gate.accepted(report | mutation))
        for key, value in (
            ("actual_concurrency_before_drain", 1),
            ("prompt_calls", 5),
            ("failed_image_rolled_back", False),
            ("e2ag", "disabled"),
        ):
            altered = deepcopy(report)
            altered["observations"][0][key] = value
            self.assertFalse(gate.accepted(altered))
