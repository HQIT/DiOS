"""Validate retained real DSH readonly probes; never invokes a model."""

import hashlib
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OBSERVATIONS = []


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def calls(path):
    result = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row["kind"] == "tool":
            value = row["value"]
            result.setdefault(value["toolCallId"], {}).update(value)
    return list(result.values())


@unittest.skipUnless(
    os.environ.get("DIOS_READONLY_EVIDENCE"), "retained real Harness evidence required"
)
class ReadonlyEvidenceTests(unittest.TestCase):
    def test_native_write_compile_package_and_egress_are_denied(self):
        root = Path(os.environ["DIOS_READONLY_EVIDENCE"]).resolve()
        old = Path(os.environ["DIOS_DSH_CONTROL_EVIDENCE"]).resolve()
        inputs = json.loads((root / "inputs.json").read_text())
        baseline = json.loads(
            (
                ROOT / "docs/design/evidence/dsh-native-controls-20261126.json"
            ).read_text()
        )
        for name in (
            "enforced-effects/protocol.jsonl",
            "native-enforced/result.json",
            "source/sample.py",
        ):
            self.assertEqual(sha(old / name), baseline["retained_sha256"][name])
        after = {name: sha(root / "source" / name) for name in inputs["source_sha256"]}
        self.assertEqual(after, inputs["source_sha256"])
        for marker, expected in inputs["positive_controls"].items():
            self.assertFalse((root / "effects" / marker).exists(), marker)
            self.assertEqual(sha(root / "positive" / marker), expected)
        self.assertEqual(
            set(inputs["positive_controls"]), {"sample.pyc", "package-ran"}
        )
        old_calls = calls(old / "enforced-effects/protocol.jsonl")
        new_calls = calls(root / "effects/protocol.jsonl")
        compile_cmd = "python -c \"import py_compile; py_compile.compile('/probe-source/sample.py', cfile='/probe-effects/sample.pyc', doraise=True)\""
        package_cmd = "node /opt/npm/bin/npm-cli.js --prefix /probe-source run build"
        selections = {
            "write": [
                c
                for c in old_calls
                if c.get("title") == "edit"
                and c["rawInput"]["file_path"] == "/probe-source/sample.py"
            ],
            "compile": [
                c
                for c in new_calls
                if c.get("title") == "bash" and c["rawInput"]["command"] == compile_cmd
            ],
            "package": [
                c
                for c in new_calls
                if c.get("title") == "bash" and c["rawInput"]["command"] == package_cmd
            ],
            "egress": [
                c
                for c in old_calls
                if c.get("title") == "web_fetch"
                and c["rawInput"]["url"]
                == "https://example.org/dios-synthetic-control-probe"
            ],
        }
        for label, rows in selections.items():
            self.assertEqual(len(rows), 1, label)
            self.assertEqual(rows[0]["status"], "failed", label)
            self.assertIn(
                "DIOS_NATIVE_TOOL_DENIED", json.dumps(rows[0]["content"]), label
            )
        runs = {}
        for label, path, trace in (
            ("write_egress", old / "native-enforced/result.json", old_calls),
            ("compile_package", root / "run/result.json", new_calls),
        ):
            result = json.loads(path.read_text())
            self.assertTrue(result["passed"])
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["result_frames"], 0)
            self.assertGreater(result["process_id"], 0)
            self.assertEqual(result["exit"]["state"], "exited")
            self.assertFalse(result["exit"]["alive"])
            auth = result["authorization_before"]
            self.assertEqual(auth["mode"], "e2ag")
            self.assertEqual(auth["grant"]["attempt_id"], result["attempt_id"])
            self.assertEqual(auth["grant"]["session_id"], result["session_id"])
            invocation = result["invocation"]
            self.assertEqual(invocation["state"], "succeeded")
            self.assertEqual(invocation["approval_state"], "consumed")
            self.assertEqual(
                invocation["arguments"]["query"], inputs["source_sha256"]["sample.py"]
            )
            bindings = {}
            for stage in ("dispatch", "receipt"):
                row = next(e for e in invocation["evidence"] if e["stage"] == stage)
                self.assertEqual(row["decision"]["decision"], "allow")
                binding = row["decision"]["evidence"]["binding"]
                for field in ("run_id", "attempt_id", "session_id"):
                    self.assertEqual(binding[field], result[field])
                    self.assertEqual(invocation[field], result[field])
                self.assertEqual(binding["authorization_ref"], auth["reference"])
                self.assertEqual(
                    binding["authorization_epoch"], auth["authorization_epoch"]
                )
                self.assertEqual(binding["lease_epoch"], auth["grant"]["lease_epoch"])
                bindings[stage] = binding
            searches = [
                c for c in trace if c.get("title") == "mcp__dios__repository_search"
            ]
            self.assertEqual(len(searches), 1)
            self.assertEqual(searches[0]["status"], "completed")
            self.assertEqual(searches[0]["rawInput"], invocation["arguments"])
            runs[label] = {
                k: result[k]
                for k in (
                    "run_id",
                    "attempt_id",
                    "session_id",
                    "process_id",
                    "image",
                    "exit_receipt_sha256",
                )
            }
            runs[label]["bindings"] = bindings
        self.assertEqual(runs["compile_package"]["image"], inputs["image"])
        OBSERVATIONS.append(
            {
                "case": "R15",
                "runs": runs,
                "source_before": inputs["source_sha256"],
                "source_after": after,
                "positive_controls": inputs["positive_controls"],
                "denied": {
                    name: rows[0]["toolCallId"] for name, rows in selections.items()
                },
                "side_effect_markers_absent": True,
                "qualification": "pinned DSH guard composition; retained real traces, no new model calls",
            }
        )
