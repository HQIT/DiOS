"""Provider failures use structured DSH facts, scoped to prompt RPCs."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from acp.exceptions import RequestError
from dios_sdk.acp import AcpClient
from dios_sdk.dsh import provider_error
from dios_sdk.protocol import WorkloadError


class DshErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_failures_keep_distinct_runtime_policy(self):
        cases = {
            "RATE_LIMIT": "PROVIDER_RATE_LIMITED",
            "QUOTA": "PROVIDER_QUOTA_EXHAUSTED",
            "ACCOUNT_QUOTA": "PROVIDER_QUOTA_EXHAUSTED",
            "TIMEOUT": "PROVIDER_TIMEOUT",
            "TRANSPORT": "PROVIDER_NETWORK_ERROR",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                peer = AcpClient(SimpleNamespace(), error_decoder=provider_error)
                peer.connection.send_request = AsyncMock(
                    side_effect=RequestError(
                        -32603, "private provider detail", {"code": source}
                    )
                )
                with self.assertRaises(WorkloadError) as caught:
                    await peer._request("session/prompt", {})
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("private", str(caught.exception))

    async def test_text_unknown_and_malformed_data_do_not_request_retry(self):
        for data in (
            None,
            "RATE_LIMIT",
            [],
            {"code": []},
            {"code": "AUTH"},
            {"code": "UNKNOWN"},
            {"message": "429 quota RATE_LIMIT"},
        ):
            with self.subTest(data=data):
                peer = AcpClient(SimpleNamespace(), error_decoder=provider_error)
                peer.connection.send_request = AsyncMock(
                    side_effect=RequestError(-32603, "429 quota RATE_LIMIT", data)
                )
                with self.assertRaisesRegex(WorkloadError, "^ACP_REQUEST_FAILED$"):
                    await peer._request("session/prompt", {})

    async def test_other_methods_and_other_harnesses_keep_existing_behavior(self):
        error = RequestError(-32603, "detail", {"code": "RATE_LIMIT"})
        for decoder, method, expected in (
            (None, "session/prompt", "ACP_REQUEST_FAILED"),
            (provider_error, "session/new", "ACP_REQUEST_FAILED"),
            (provider_error, "authenticate", "ACP_AUTH_FAILED"),
        ):
            peer = AcpClient(SimpleNamespace(), error_decoder=decoder)
            peer.connection.send_request = AsyncMock(side_effect=error)
            with self.assertRaisesRegex(WorkloadError, "^" + expected + "$"):
                await peer._request(method, {})
        self.assertIsNone(
            provider_error(RequestError(-32602, "detail", {"code": "RATE_LIMIT"}))
        )


if __name__ == "__main__":
    unittest.main()
