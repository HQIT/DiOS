import unittest

import test_sdk as fixtures
from dios_sdk import TaskClient, WorkloadError


class ApplicationClientTests(unittest.TestCase):
    setUp = fixtures.ClientTests.setUp
    tearDown = fixtures.ClientTests.tearDown

    def test_explicit_contract_requires_server_advertisement(self):
        strict = TaskClient(self.url, "private-token", required_contract="1")
        with self.assertRaisesRegex(WorkloadError, "RUNTIME_CONTRACT_UNSUPPORTED"):
            strict.runtime_contract()
        self.headers = {"X-DiOS-Runtime-Contract": "2"}
        with self.assertRaisesRegex(WorkloadError, "RUNTIME_CONTRACT_UNSUPPORTED"):
            strict.runtime_contract()
        self.headers = {"X-DiOS-Runtime-Contract": "1"}
        self.body = {"contract_version": "1"}
        self.assertEqual(strict.runtime_contract()["contract_version"], "1")

    def test_application_preview_is_read_only_and_carries_exact_revision(self):
        self.body = {
            "compatible": False,
            "issues": [{"code": "APPLICATION_INTERFACE_UNSUPPORTED"}],
        }
        candidate = {"requires": {"runtime-api": ">=2"}}
        result = self.client.application_compatibility("rev-exact", contract=candidate)
        self.assertFalse(result["compatible"])
        self.assertEqual(
            self.requests[0][1], "/api/os/runtime/applications/compatibility"
        )
        self.assertEqual(
            self.requests[0][2], {"revision_ref": "rev-exact", "contract": candidate}
        )
        self.client.applications()
        self.assertEqual(self.requests[1][0], "GET")
        self.assertEqual(self.requests[1][1], "/api/os/runtime/applications")

    def test_legacy_client_remains_explicit_and_unknown_contract_rejected(self):
        self.assertEqual(self.client.get("old")["state"], "running")
        with self.assertRaises(ValueError):
            TaskClient(self.url, "private-token", required_contract="2")

    def test_old_server_is_detected_before_mutating_submission(self):
        strict = TaskClient(self.url, "private-token", required_contract="1")
        with self.assertRaisesRegex(WorkloadError, "RUNTIME_CONTRACT_UNSUPPORTED"):
            strict.submit("profile", "sample", "goal", idempotency_key="stable")
        self.assertEqual([r[0] for r in self.requests], ["GET"])
        self.assertEqual(self.requests[0][1], "/api/os/runtime/contracts")
