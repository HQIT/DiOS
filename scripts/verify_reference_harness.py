"""Verify retained reference outcomes and unchanged scenario-specific application code."""

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def scenario_source_digest(source, scenario):
    """Other scenarios' literal prompt rules do not affect this execution path."""
    tree = ast.parse(source)
    found = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "RULES" for t in node.targets
        ):
            assert isinstance(node.value, ast.Dict)
            pairs = [
                (k, v)
                for k, v in zip(node.value.keys, node.value.values)
                if isinstance(k, ast.Constant) and k.value == scenario
            ]
            assert len(pairs) == 1
            node.value.keys, node.value.values = [pairs[0][0]], [pairs[0][1]]
            found = True
    assert found
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def verify(path):
    report = json.loads(path.read_text())
    assert report["work_item"] == "F03-2.03" and report["complete_work_item"] is False
    cases = {(c["harness"], c["scenario"]): c for c in report["cases"]}
    assert len(cases) == 6 and set(cases) == {
        (h, s) for h in ("cursor", "dsh") for s in ("sales", "team", "education")
    }
    fixture = json.loads(
        (ROOT / "docs/design/fixtures/reference-scenarios.json").read_text()
    )
    expected = {row["id"]: row for row in fixture["scenarios"]}
    source = (
        ROOT / "applications/reference-workflows/src/dios_reference/harness.py"
    ).read_text()
    identities = []
    for key, case in cases.items():
        raw = (ROOT / case["artifact"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == case["sha256"]
        row = json.loads(raw)
        assert (
            row["passed"]
            and row["state"] == "succeeded"
            and row["image"] == case["image"]
        )
        assert scenario_source_digest(source, key[1]) == case["scenario_source_sha256"]
        result = row["output"]
        assert result["application_version"] == "0.1.1"
        assert case["versions"]["dios-sdk"] == "0.1.20"
        sample = json.loads(
            (
                ROOT / "examples/reference-workflows" / (key[1] + "-inputs.json")
            ).read_text()
        )["inputs"][0]
        assert result["input_digest"] == sample["sha256"] == row["input_digest"]
        assert result["review_status"] == "pending_review"
        assert {c["id"]: c["digest"] for c in result["citations"]} == sample[
            "source_refs"
        ]
        invocation, attempt = row["invocation"], row["attempt"]
        assert (
            invocation["attempt_id"] == attempt["id"]
            and invocation["session_id"] == attempt["session_id"]
        )
        assert (
            invocation["state"] == "succeeded"
            and invocation["approval_state"] == "consumed"
        )
        assert row["authorization"]["mode"] == "e2ag" and row["revision"].startswith(
            "rev-"
        )
        assert not row["receipt"]["observation"]["alive"]
        outcome = result["outcome"]
        if key[1] == "sales":
            assert [x["id"] for x in outcome["recommended"]] == [
                expected["sales"]["expected"]["recommended"]
            ]
            assert [x["id"] for x in outcome["excluded"]] == [
                expected["sales"]["expected"]["excluded"]
            ]
        elif key[1] == "team":
            assert outcome["plans"] == expected["team"]["input"]["plans"]
            assert outcome["needs_owner"] == expected["team"]["expected"]["needs_owner"]
        else:
            assert (
                outcome["source_ids"]
                == expected["education"]["expected"]["student_artifact_sources"]
            )
            assert "answer-1" not in json.dumps(result) and expected["education"][
                "input"
            ]["answer"]["text"] not in json.dumps(result, ensure_ascii=False)
        identities.append((row["run_id"], attempt["id"], attempt["session_id"]))
    assert all(len({row[i] for row in identities}) == 6 for i in range(3))
    for failure in report["rejected"]:
        raw = (ROOT / failure["artifact"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == failure["sha256"]
        value = json.loads(raw)
        encoded = b"".join(
            (
                json.dumps(
                    frame,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode()
            for frame in value["frames"]
        )
        assert hashlib.sha256(encoded).hexdigest() == value["stdout_sha256"]
        assert not any(frame.get("type") == "result" for frame in value["frames"])
        if failure["kind"] == "model-result-rejected":
            assert value["attempt"]["error"]["code"] == "EVIDENCE_INSUFFICIENT"
    for filename, sha in report["shared_sources"].items():
        assert hashlib.sha256((ROOT / filename).read_bytes()).hexdigest() == sha, (
            filename
        )
    assert report["remaining"] == [
        "G05 human takeover and late-result review",
        "G06 cross-implementation WorkItem handoff",
        "F03-2.02 full reference fault qualification",
    ]
    print(
        json.dumps(
            {
                "passed_reference_paths": 6,
                "complete_work_item": False,
                "remaining": report["remaining"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    verify(parser.parse_args().report)
