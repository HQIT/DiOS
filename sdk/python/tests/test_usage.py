"""ACP telemetry normalizes only documented counters and bounded cost values."""

import unittest

import test_acp_contract as fixtures
from dios_sdk.usage import usage_frame


class UsageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AcpBridgeTests.asyncSetUp

    async def test_usage_notification_keeps_context_separate_and_drops_metadata(self):
        await self.peer.incoming(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "session",
                    "update": {
                        "sessionUpdate": "usage_update",
                        "used": 123,
                        "size": 200000,
                        "cost": {"amount": 0.125, "currency": "USD"},
                        "_meta": {"secret": "not emitted"},
                    },
                },
            }
        )
        self.assertEqual(
            self.events,
            [
                usage_frame(
                    "session",
                    1,
                    "acp_session",
                    {
                        "used": 123,
                        "size": 200000,
                        "cost": {"amount": 0.125, "currency": "USD"},
                    },
                )
            ],
        )
        self.assertNotIn("secret", str(self.events))

    def test_invalid_nonfinite_boolean_and_negative_values_remain_unknown(self):
        for value in [
            {"used": None, "size": 200},
            {"used": True, "size": 200},
            {"used": -1, "size": 200},
            {"used": 1, "size": 200, "cost": {"amount": "1e-1000000000", "currency": "USD"}},
            {"used": 1, "size": 200, "cost": {"amount": "0e1000000000", "currency": "USD"}},
            {
                "used": 1,
                "size": 200,
                "cost": {"amount": float("nan"), "currency": "USD"},
            },
        ]:
            frame = usage_frame("session", 1, "acp_session", value)
            self.assertEqual(frame["quality"], "invalid")
            self.assertEqual(frame["values"], {})

    def test_token_total_is_not_recomputed_from_overlapping_subtypes(self):
        frame = usage_frame(
            "session",
            2,
            "acp_prompt",
            {
                "inputTokens": 20,
                "outputTokens": 10,
                "totalTokens": 30,
                "thoughtTokens": 7,
                "cachedReadTokens": 15,
            },
        )
        self.assertEqual(frame["values"]["totalTokens"], 30)
