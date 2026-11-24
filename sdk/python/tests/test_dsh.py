"""DSH-specific metadata and shared ACP capability contracts."""

import asyncio
import json
import unittest
from types import SimpleNamespace

from dios_sdk.acp import AcpClient
from dios_sdk.acp_result import final_object
from dios_sdk.dsh import proxy_permission, tool_activity
from dios_sdk.protocol import WorkloadError


class DshMetadataTests(unittest.TestCase):
    def test_only_dios_proxy_tools_are_mapped(self):
        valid = {"title": "mcp__dios__repository_search", "rawInput": {"query": "sha"}}
        self.assertEqual(
            proxy_permission(valid),
            {"name": valid["title"], "arguments": {"query": "sha"}},
        )
        for title in (
            "bash",
            "read_file",
            "mcp__other__repository_search",
            "mcp__dios__search/../bash",
            "",
        ):
            with self.subTest(title=title), self.assertRaises(WorkloadError):
                proxy_permission(dict(valid, title=title))
        with self.assertRaises(WorkloadError):
            proxy_permission(dict(valid, rawInput="{}"))

    def test_incomplete_activity_cannot_be_accepted(self):
        call = {"title": "mcp__dios__repository_search", "rawInput": {"query": "sha"}}
        for status in ("pending", "in_progress", None):
            with self.subTest(status=status), self.assertRaises(WorkloadError):
                tool_activity(dict(call, status=status))
        for status in ("completed", "failed"):
            self.assertEqual(
                tool_activity(dict(call, status=status))["rawInput"], {"query": "sha"}
            )

    def test_final_result_retains_unicode_and_rejects_truncation(self):
        self.assertEqual(
            final_object('说明\n```json\n{"标题":"中文🙂"}\n```'), {"标题": "中文🙂"}
        )
        for text in ('{"partial":', '{"ok":1}\nunfinished', "[1,2]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                final_object(text)


class DshPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_id_only_permission_uses_prior_session_tool_evidence(self):
        class Writer:
            def __init__(self):
                self.messages = []

            def write(self, data):
                self.messages.append(json.loads(data))

            async def drain(self):
                pass

        class Gate:
            def __init__(self):
                self.calls = []

            async def request(self, method, params):
                self.calls.append((method, params))
                return {"forward_to_gate": True}

        writer, gate = Writer(), Gate()
        peer = AcpClient(
            SimpleNamespace(stdin=writer),
            tool_client=gate,
            permission_decoder=proxy_permission,
        )
        peer.session_id = "one"
        await peer.incoming(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "one",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "call-1",
                        "title": "mcp__dios__repository_search",
                        "rawInput": {"query": "sha"},
                        "status": "in_progress",
                    },
                },
            }
        )
        options = [
            {"kind": "allow_once", "optionId": "yes"},
            {"kind": "reject_once", "optionId": "no"},
        ]
        params = {
            "sessionId": "one",
            "toolCall": {"toolCallId": "call-1"},
            "options": options,
        }
        result = await peer.request("session/request_permission", params, 1)
        self.assertEqual(result["outcome"]["optionId"], "yes")
        self.assertEqual(
            gate.calls,
            [
                (
                    "permit_proxy",
                    {
                        "name": "mcp__dios__repository_search",
                        "arguments": {"query": "sha"},
                    },
                )
            ],
        )
        with self.assertRaisesRegex(WorkloadError, "SESSION_CHANGED"):
            await peer.request(
                "session/request_permission", dict(params, sessionId="other"), 2
            )
        result = await peer.request(
            "session/request_permission",
            dict(params, toolCall={"toolCallId": "unknown"}),
            3,
        )
        self.assertEqual(result["outcome"]["optionId"], "no")

    async def test_close_requires_advertisement_and_still_runs_after_cancellation(self):
        peer = AcpClient(SimpleNamespace())
        peer.session_id = "one"
        calls = []

        async def request(method, params):
            calls.append((method, params))
            return {}

        peer._request = request
        await peer.close_session()
        self.assertFalse(calls)
        peer.session_close_supported = True
        peer.cancelling = True
        await peer.close_session()
        self.assertEqual(calls, [("session/close", {"sessionId": "one"})])

    async def test_close_timeout_is_bounded(self):
        peer = AcpClient(SimpleNamespace())
        peer.session_id = "one"
        peer.session_close_supported = True

        async def request(*args):
            await asyncio.Event().wait()

        peer._request = request
        await asyncio.wait_for(peer.close_session(timeout=0.01), 0.2)


if __name__ == "__main__":
    unittest.main()
