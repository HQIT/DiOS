"""Original scenario expectations and adverse evidence against installed packages."""

import copy
import json
import unittest
from pathlib import Path

from dios_reference.app import result
from dios_sdk import WorkloadError, digest

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "examples/reference-workflows"
CASES = {
    c["id"]: c
    for c in json.loads(
        (ROOT / "docs/design/fixtures/reference-scenarios.json").read_text()
    )["scenarios"]
}


def fixture(scenario):
    value = json.loads((EXAMPLES / (scenario + "-inputs.json")).read_text())["inputs"][
        0
    ]
    documents = [
        c
        for c in json.loads((EXAMPLES / "catalog.json").read_text())["documents"]
        if c["project_id"] == value["context_id"] and c["id"] in value["source_refs"]
    ]
    data = {
        "project_id": value["context_id"],
        "query": value["query"],
        "documents": documents,
        "retrieval": "literal-terms-v1",
        "truncated": False,
    }
    return value, data


def response(data):
    return {
        "isError": False,
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
    }


def stamp(value):
    value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})


class ReferenceWorkflows(unittest.TestCase):
    def test_sales_matches_original_expectation(self):
        value, data = fixture("sales")
        output = result(value, response(data), "选择合适规格")
        self.assertEqual(
            [x["id"] for x in output["outcome"]["recommended"]],
            [CASES["sales"]["expected"]["recommended"]],
        )
        self.assertEqual(
            [x["id"] for x in output["outcome"]["excluded"]],
            [CASES["sales"]["expected"]["excluded"]],
        )
        self.assertEqual(len(output["citations"]), 2)
        self.assertEqual(output["review_status"], "pending_review")

    def test_changed_constraints_change_recommendation(self):
        value, data = fixture("sales")
        value["constraints"]["battery_hours_min"] = 6
        stamp(value)
        output = result(value, response(data), "要求六小时续航")["outcome"]
        self.assertEqual(output["recommended"], [])
        self.assertEqual({x["id"] for x in output["excluded"]}, {"spec-A", "spec-B"})

    def test_team_retains_sources_and_missing_owner(self):
        value, data = fixture("team")
        output = result(value, response(data), "汇总岗位计划")["outcome"]
        expected = CASES["team"]["expected"]
        self.assertEqual(output["sources_preserved"], expected["sources_preserved"])
        self.assertEqual(output["needs_owner"], expected["needs_owner"])
        self.assertEqual(output["plans"], CASES["team"]["input"]["plans"])

    def test_student_report_has_only_lecture(self):
        value, data = fixture("education")
        output = result(value, response(data), "生成学生导读")
        self.assertEqual(
            output["outcome"]["source_ids"],
            CASES["education"]["expected"]["student_artifact_sources"],
        )
        raw = json.dumps(output, ensure_ascii=False)
        self.assertNotIn("answer-1", raw)
        self.assertNotIn(CASES["education"]["input"]["answer"]["text"], raw)

    def test_changed_source_version_rejected(self):
        value, data = fixture("sales")
        data["documents"][0]["version"] = "different-version"
        row = data["documents"][0]
        row["digest"] = digest({k: v for k, v in row.items() if k != "digest"})
        with self.assertRaisesRegex(WorkloadError, "EVIDENCE_INSUFFICIENT"):
            result(value, response(data), "核验来源")

    def test_input_tamper_and_wrong_context_rejected(self):
        value, data = fixture("sales")
        value["context_id"] = "another-context"
        with self.assertRaisesRegex(WorkloadError, "INPUT_DIGEST_MISMATCH"):
            result(value, response(data), "核验")
        stamp(value)
        with self.assertRaisesRegex(WorkloadError, "EVIDENCE_INSUFFICIENT"):
            result(value, response(data), "核验")

    def test_teacher_material_cannot_become_student_report(self):
        value, data = fixture("education")
        answer = next(
            d
            for d in json.loads((EXAMPLES / "catalog.json").read_text())["documents"]
            if d["id"] == "answer-1"
        )
        data["documents"].append(answer)
        with self.assertRaisesRegex(WorkloadError, "EVIDENCE_INSUFFICIENT"):
            result(value, response(data), "核验学生材料")
        value["source_refs"][answer["id"]] = answer["digest"]
        stamp(value)
        with self.assertRaisesRegex(WorkloadError, "EVIDENCE_INSUFFICIENT"):
            result(value, response(data), "核验学生材料")

    def test_missing_or_truncated_evidence_rejected(self):
        value, data = fixture("team")
        for change in ("missing", "truncated"):
            altered = copy.deepcopy(data)
            if change == "missing":
                altered["documents"].pop()
            else:
                altered["truncated"] = True
            with self.assertRaisesRegex(WorkloadError, "EVIDENCE_INSUFFICIENT"):
                result(value, response(altered), "核验覆盖")

    def test_markdown_goal_is_data(self):
        value, data = fixture("sales")
        output = result(value, response(data), "![load](https://example.invalid)")
        self.assertNotIn("![load]", output["markdown"])


if __name__ == "__main__":
    unittest.main()
