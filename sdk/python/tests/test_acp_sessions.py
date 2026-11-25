import asyncio
import json
import unittest
from types import SimpleNamespace

from dios_sdk.acp_sessions import AcpSessionInspector, session_capabilities
from dios_sdk.protocol import WorkloadError


class Writer:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        pass

    def close(self):
        pass


class CapabilitiesTests(unittest.TestCase):
    def test_capabilities_are_explicit_and_do_not_imply_turn_resume(self):
        self.assertEqual(
            session_capabilities({"protocolVersion": 1}),
            {"load": False, "resume": False, "list": False, "close": False},
        )
        caps = session_capabilities(
            {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": "true",
                    "sessionCapabilities": {"resume": {}, "close": True},
                },
            }
        )
        self.assertEqual(
            caps, {"load": False, "resume": True, "list": False, "close": False}
        )

    def test_invalid_negotiation_fails_explicitly(self):
        for value in (
            None,
            {},
            {"protocolVersion": True},
            {"protocolVersion": 2},
            {"protocolVersion": 1, "agentCapabilities": []},
            {"protocolVersion": 1, "agentCapabilities": {"sessionCapabilities": None}},
        ):
            with self.subTest(value=value), self.assertRaises(WorkloadError):
                session_capabilities(value)


class InspectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.process = SimpleNamespace(
            stdin=Writer(), stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader()
        )
        self.peer = AcpSessionInspector(self.process)
        self.peer.reader = asyncio.create_task(self.peer.read())
        self.init = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}

    async def asyncTearDown(self):
        await self.peer.connection.close()
        self.peer.reader.cancel()
        await asyncio.gather(
            self.peer.reader, *self.peer.pending, return_exceptions=True
        )

    def frame(self, value):
        self.process.stdout.feed_data(
            (json.dumps({"jsonrpc": "2.0", **value}) + "\n").encode()
        )

    async def restore_request(self, *, timeout=1):
        task = asyncio.create_task(
            self.peer.inspect(self.init, "original", "/tmp", timeout=timeout)
        )
        for _ in range(100):
            if self.process.stdin.messages:
                break
            await asyncio.sleep(0.001)
        self.assertTrue(self.process.stdin.messages)
        return task, self.process.stdin.messages[0]

    def update(self, kind, **fields):
        return {
            "method": "session/update",
            "params": {
                "sessionId": "original",
                "update": {"sessionUpdate": kind, **fields},
            },
        }

    async def test_history_tool_records_never_become_live_execution_or_output(self):
        emitted = []
        self.peer.emit = emitted.append
        task, request = await self.restore_request()
        self.assertEqual(
            request["params"],
            {"sessionId": "original", "cwd": "/tmp", "mcpServers": []},
        )
        self.frame(
            self.update(
                "agent_message_chunk", content={"type": "text", "text": "旧结果🙂"}
            )
        )
        self.frame(
            self.update("tool_call", toolCallId="old", rawInput={"query": "old"})
        )
        self.frame(self.update("usage_update", used=100, size=1000))
        self.frame({"id": request["id"], "result": {}})
        await task
        snapshot = self.peer.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(len(snapshot["history"]), 3)
        self.assertEqual(emitted, [])
        self.assertEqual(self.peer.usage_sequence, 0)
        self.assertFalse(snapshot["execution_resumed"])
        self.assertEqual(self.peer.output, "")
        self.assertEqual(self.peer.tool_activity, {})
        self.assertFalse(self.peer.native_tool_seen)
        for method in ("session/prompt", "session/new", "session/load", "authenticate"):
            with self.assertRaisesRegex(WorkloadError, "ACP_INSPECTION_ONLY"):
                await self.peer.call(method, {})
        self.assertEqual(len(self.process.stdin.messages), 1)

    async def test_all_callbacks_denied_without_tools_or_approval(self):
        task, request = await self.restore_request()
        for i, method in enumerate(
            ("session/request_permission", "fs/read_text_file", "terminal/create"), 40
        ):
            self.frame({"id": i, "method": method, "params": {"sessionId": "original"}})
        self.frame({"id": request["id"], "result": {}})
        await task
        for _ in range(100):
            if len(self.process.stdin.messages) == 4:
                break
            await asyncio.sleep(0.001)
        replies = {x["id"]: x for x in self.process.stdin.messages if "method" not in x}
        self.assertEqual(replies[40]["result"]["outcome"]["outcome"], "cancelled")
        self.assertEqual(replies[41]["error"]["code"], -32601)
        self.assertEqual(replies[42]["error"]["code"], -32601)
        self.assertEqual(self.peer.denials, 3)

    async def test_resume_without_history_is_negotiated_separately(self):
        self.init["agentCapabilities"] = {"sessionCapabilities": {"resume": {}}}
        task, request = await self.restore_request()
        self.assertEqual(request["method"], "session/resume")
        self.frame({"id": request["id"], "result": {}})
        await task
        self.assertEqual(self.peer.snapshot()["history"], [])

    async def test_unsupported_restore_sends_nothing(self):
        for method in (None, "load", "resume", "prompt"):
            with self.assertRaisesRegex(
                WorkloadError, "ACP_SESSION_RESTORE_UNSUPPORTED"
            ):
                await self.peer.inspect(
                    {"protocolVersion": 1}, "original", "/tmp", method=method
                )
        self.assertEqual(self.process.stdin.messages, [])

    async def test_absolute_windows_working_directory_and_invalid_paths(self):
        for cwd in ("relative", "C:relative", "", "/tmp\x00bad", None):
            with self.assertRaisesRegex(WorkloadError, "ACP_CWD_INVALID"):
                await self.peer.inspect(self.init, "original", cwd)
        task = asyncio.create_task(
            self.peer.inspect(self.init, "original", r"C:\isolated")
        )
        for _ in range(100):
            if self.process.stdin.messages:
                break
            await asyncio.sleep(0.001)
        request = self.process.stdin.messages[0]
        self.assertEqual(request["params"]["cwd"], r"C:\isolated")
        self.frame({"id": request["id"], "result": {}})
        await task

    async def test_timeout_keeps_late_history_separate_and_forbids_retry(self):
        task, _request = await self.restore_request(timeout=0.02)
        with self.assertRaisesRegex(WorkloadError, "ACP_QUERY_TIMEOUT"):
            await task
        self.frame(
            self.update("agent_message_chunk", content={"type": "text", "text": "late"})
        )
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.peer.history), 1)
        self.assertFalse(self.peer.snapshot()["complete"])
        self.assertEqual(self.peer.output, "")
        with self.assertRaisesRegex(WorkloadError, "ACP_INSPECTION_ALREADY_STARTED"):
            await self.peer.inspect(self.init, "original", "/tmp")

    async def test_wrong_session_is_rejected_before_history_acceptance(self):
        task, _request = await self.restore_request()
        message = self.update("agent_message_chunk")
        message["params"]["sessionId"] = "other"
        self.frame(message)
        with self.assertRaisesRegex(WorkloadError, "SESSION_CHANGED"):
            await task
        self.assertEqual(self.peer.history, [])

    async def test_history_limits_are_utf8_bytes_and_event_count(self):
        self.peer.session_id = "original"
        for limit in ("history_limit", "event_limit"):
            setattr(self.peer, limit, 0)
            with self.assertRaisesRegex(WorkloadError, "ACP_HISTORY_LIMIT_EXCEEDED"):
                await self.peer.incoming(
                    self.update(
                        "agent_message_chunk", content={"type": "text", "text": "中文"}
                    )
                )
            setattr(self.peer, limit, 4096)
        self.assertEqual(self.peer.history_bytes, 0)


if __name__ == "__main__":
    unittest.main()
