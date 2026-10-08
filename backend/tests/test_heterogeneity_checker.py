"""Checker sensitivity against the completed smoke artifact, without edits."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/e2ag/heterogeneity"))
import check_paired_matrix as checker


class EvidenceCheckerTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / "experiments/e2ag/heterogeneity/results/paired-smoke-20261007-v1/R0/normal"
        self.raw = checker.tables(self.folder, "sqlite")

    def audit(self, raw):
        with patch.object(checker, "tables", return_value=raw):
            return checker.audit_case(self.folder, "normal", "http", "streamable_http", "sqlite")[0]

    def test_unchanged_artifact(self):
        self.assertEqual([], self.audit(copy.deepcopy(self.raw)))

    def test_broken_grant_binding(self):
        changed = copy.deepcopy(self.raw)
        changed["grants"][0]["trace_id"] = "unrelated-trace"
        self.assertIn("grant_binding", self.audit(changed))

    def test_wrong_permission_snapshot(self):
        changed = copy.deepcopy(self.raw)
        changed["grants"][0]["allowed_tools"] = ["other.write"]
        self.assertIn("grant_scope_snapshot", self.audit(changed))

    def test_missing_intent_despite_rehashed_chain(self):
        import hashlib
        import json
        changed = copy.deepcopy(self.raw)
        chain = changed["events"][0]["audit_chain"]
        chain[:] = [e for e in chain if e["stage"] != "tool_intent"]
        previous = ""
        for index, entry in enumerate(chain):
            entry.pop("entry_hash")
            entry.update(sequence=index, previous_hash=previous)
            previous = hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            entry["entry_hash"] = previous
        problems = self.audit(changed)
        self.assertNotIn("audit_hash_link", problems)
        self.assertIn("tool_intent_evidence", problems)

    def test_unexpected_tool_write(self):
        original = checker.lines
        def read(path):
            result = original(path)
            if path.name == "tool-receipts.jsonl": result = result + copy.deepcopy(result)
            return result
        with patch.object(checker, "lines", side_effect=read):
            self.assertIn("tool_artifact_count", self.audit(copy.deepcopy(self.raw)))


if __name__ == "__main__":
    unittest.main()
