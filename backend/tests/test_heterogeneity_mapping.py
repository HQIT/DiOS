"""Supplementary adapter-contract checks, not complete workflow runs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments/e2ag/heterogeneity"))
from file_ingress import normalize_envelope, read_ready_event, IngressError
from http_ingress import decode, signature


class MappingTests(unittest.TestCase):
    def test_same_authorization_fields_across_adapters(self):
        body = {"id": "e-1", "subject": "task", "data": {"source": "untrusted/nested", "environment": "production", "requested_action": "report.create"}}
        raw = json.dumps(body).encode()
        events = [normalize_envelope(raw, "fixture", mechanism) for mechanism in ["http", "amqp", "file"]]
        self.assertEqual(1, len({e["deliverydigest"] for e in events}))
        for mechanism, event in zip(["http", "amqp", "file"], events):
            self.assertEqual(mechanism + "/fixture", event["source"])
            self.assertEqual(body["data"], event["data"])
            self.assertEqual(body["id"], event["id"])

    def test_signature_covers_body_and_required_secret(self):
        raw = b'{"id":"e1","subject":"s","data":{}}'
        supplied = signature(raw, "local-test-secret")
        self.assertEqual("http/fixture", decode(raw, supplied, "local-test-secret")["source"])
        for changed, secret in [(raw + b" ", "local-test-secret"), (raw, ""), (raw, "wrong")]:
            with self.assertRaisesRegex(IngressError, "INVALID_HTTP_SIGNATURE"):
                decode(changed, supplied, secret)

    @unittest.skipUnless(os.name == "nt", "Windows junction/reparse boundary")
    def test_actual_junction_inbox_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, junction = root / "target", root / "linked"
            target.mkdir()
            (target / "input.ready.json").write_text('{"id":"e1","subject":"s","data":{}}')
            # Paths are test-owned temporary children, passed via environment; no
            # command string is constructed from external event data.
            result = subprocess.run(["powershell", "-NoProfile", "-Command",
                "New-Item -ItemType Junction -Path $env:E2AG_TEST_LINK -Target $env:E2AG_TEST_TARGET | Out-Null"],
                capture_output=True, text=True, env={**os.environ, "E2AG_TEST_LINK": str(junction), "E2AG_TEST_TARGET": str(target)})
            try:
                self.assertEqual(0, result.returncode, result.stderr)
                with self.assertRaisesRegex(IngressError, "LINKED_INBOX"):
                    read_ready_event(junction, "input.ready.json", "fixture")
            finally:
                if junction.exists():
                    os.rmdir(junction)  # remove link itself, never target tree


if __name__ == "__main__":
    unittest.main()
