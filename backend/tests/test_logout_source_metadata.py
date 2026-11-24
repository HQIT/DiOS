import asyncio
import tempfile
from pathlib import Path
import unittest
from app.runtime.oauth_source import OAuthSource
from app.runtime.database import RuntimeErrorCode
from test_runtime_oauth_source import SourceFixture


class SourceMetadataTests(SourceFixture, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.configure(Path(self.tmp.name))
        self.source = OAuthSource(self.config, transport=self.transport)

    def test_session_identity_is_bound_to_trusted_introspection(self):
        self.response.update(sid="native-session", auth_time=self.expiry - 1000)
        value = asyncio.run(self.source.inspect_async(self.access))
        self.assertEqual(value["session_id"], "native-session")
        self.assertEqual(value["session_started_at"], self.expiry - 1000)
        self.reference.update(
            session_id=value["session_id"],
            session_started_at=value["session_started_at"],
        )
        self.source.current(self.reference)
        for key, value in [("sid", "other"), ("auth_time", self.expiry - 999)]:
            original = self.response[key]
            self.response[key] = value
            with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
                self.source.current(self.reference)
            self.response[key] = original

    def test_refresh_issue_time_never_becomes_authentication_time(self):
        self.response["iat"] = self.expiry - 901
        value = asyncio.run(self.source.inspect_async(self.access))
        self.assertNotIn("session_started_at", value)
        self.assertNotIn("session_id", value)
