"""Cancellation and bounded transport contracts against the official ACP SDK."""

import asyncio
import json
from types import SimpleNamespace
import unittest

from dios_sdk.acp import AcpClient
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


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.process = SimpleNamespace(
            stdin=Writer(), stdout=asyncio.StreamReader(limit=256 * 1024)
        )
        self.events = []
        self.peer = AcpClient(self.process, emit_event=self.events.append)
        self.peer.session_id = "one"
        self.peer.reader = asyncio.create_task(self.peer.read())

    async def asyncTearDown(self):
        await self.peer.connection.close()
        self.peer.reader.cancel()
        await asyncio.gather(
            self.peer.reader, *self.peer.pending, return_exceptions=True
        )

    def feed(self, **message):
        self.process.stdout.feed_data(
            json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n"
        )

    async def until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0.001)

    async def start_prompt(self):
        task = asyncio.create_task(
            self.peer.call("session/prompt", {"sessionId": "one"}, timeout=20)
        )
        await self.until(lambda: self.process.stdin.messages)
        return task, self.process.stdin.messages[0]["id"]

    async def test_cancel_preserves_prompt_and_late_output_with_blocked_permission(
        self,
    ):
        entered = asyncio.Event()

        class Gate:
            async def request(self, *args):
                entered.set()
                await asyncio.Event().wait()

        self.peer.tool_client = Gate()
        self.peer.permission_decoder = lambda value: {}
        prompt, key = await self.start_prompt()
        self.feed(
            id="permit",
            method="session/request_permission",
            params={
                "sessionId": "one",
                "toolCall": {},
                "options": [{"kind": "allow_once", "optionId": "yes"}],
            },
        )
        await asyncio.wait_for(entered.wait(), 1)
        prompt.cancel()
        await asyncio.gather(prompt, return_exceptions=True)
        cancel = asyncio.create_task(self.peer.cancel())
        await self.until(
            lambda: any(
                m.get("method") == "session/cancel" for m in self.process.stdin.messages
            )
        )
        self.feed(
            method="session/update",
            params={
                "sessionId": "one",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "保留现场🙂"},
                },
            },
        )
        self.feed(id=key, result={"stopReason": "cancelled"})
        self.assertTrue(await cancel)
        await self.until(
            lambda: any(m.get("id") == "permit" for m in self.process.stdin.messages)
        )
        reply = next(m for m in self.process.stdin.messages if m.get("id") == "permit")
        self.assertEqual(reply["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(self.peer.output, "保留现场🙂")
        self.assertFalse(
            any(
                m.get("method") == "$/cancel_request"
                for m in self.process.stdin.messages
            )
        )

    async def test_timeout_does_not_lose_original_prompt(self):
        with self.assertRaisesRegex(WorkloadError, "ACP_QUERY_TIMEOUT"):
            await self.peer.call("session/prompt", {}, timeout=0.01)
        key = self.process.stdin.messages[0]["id"]
        cancel = asyncio.create_task(self.peer.cancel())
        await self.until(lambda: len(self.process.stdin.messages) > 1)
        self.feed(id=key, result={"stopReason": "cancelled"})
        self.assertTrue(await cancel)

    async def test_eof_cannot_acknowledge_cancel(self):
        prompt, _ = await self.start_prompt()
        cancel = asyncio.create_task(self.peer.cancel())
        self.process.stdout.feed_eof()
        self.assertFalse(await cancel)
        with self.assertRaisesRegex(WorkloadError, "ACP_PROCESS_EXITED"):
            await prompt

    async def test_cancel_timeout_and_completion_race_are_not_acknowledgement(self):
        prompt, key = await self.start_prompt()
        self.assertFalse(await self.peer.cancel(timeout=0.01))
        self.feed(id=key, result={"stopReason": "end_turn"})
        self.assertEqual((await prompt)["stopReason"], "end_turn")
        self.assertFalse(self.peer.cancel_acknowledged)

    async def test_cancelled_peer_never_enters_new_gate(self):
        self.peer.cancelling = True
        self.peer.tool_client = object()
        self.feed(
            id=7, method="session/request_permission", params={"sessionId": "one"}
        )
        await self.until(lambda: self.process.stdin.messages)
        self.assertEqual(
            self.process.stdin.messages[0]["result"]["outcome"]["outcome"], "cancelled"
        )

    async def test_blocked_read_does_not_block_response_and_has_own_key(self):
        seen = []

        class Gate:
            async def call(self, *args):
                seen.append(args)
                await asyncio.Event().wait()

        self.peer.tool_client = Gate()
        self.peer.client_read_tool = "read"
        prompt, key = await self.start_prompt()
        for n in (10, "10"):
            self.feed(
                id=n,
                method="fs/read_text_file",
                params={"sessionId": "one", "path": "/repo/x"},
            )
        await self.until(lambda: len(seen) == 2)
        self.assertEqual(len({x[2] for x in seen}), 2)
        self.feed(id=key, result={"stopReason": "end_turn"})
        self.assertEqual((await asyncio.wait_for(prompt, 1))["stopReason"], "end_turn")

    async def test_request_flood_is_bounded(self):
        class Gate:
            async def call(self, *args):
                await asyncio.Event().wait()

        self.peer.tool_client = Gate()
        self.peer.client_read_tool = "read"
        prompt, _ = await self.start_prompt()
        for n in range(17):
            self.feed(
                id=n,
                method="fs/read_text_file",
                params={"sessionId": "one", "path": "/repo/x"},
            )
        with self.assertRaisesRegex(WorkloadError, "ACP_REQUEST_LIMIT_EXCEEDED"):
            await prompt
        self.assertLessEqual(len(self.peer.transport.requests), 16)

    async def test_oversized_frame_terminates_without_accumulating(self):
        prompt, _ = await self.start_prompt()
        self.process.stdout.feed_data(b"x" * (256 * 1024 + 1))
        with self.assertRaisesRegex(WorkloadError, "ACP_FRAME_TOO_LARGE"):
            await asyncio.wait_for(prompt, 1)

    async def test_session_mismatch_fails_pending_prompt(self):
        prompt, _ = await self.start_prompt()
        self.feed(method="session/update", params={"sessionId": "other", "update": {}})
        with self.assertRaisesRegex(WorkloadError, "SESSION_CHANGED"):
            await prompt

    async def test_invalid_boolean_response_id_is_rejected(self):
        prompt, _ = await self.start_prompt()
        self.feed(id=True, result={})
        with self.assertRaisesRegex(WorkloadError, "ACP_FRAME_INVALID"):
            await prompt


class ProcessCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_waits_for_cancel_ack_and_preserves_partial_output(self):
        import os
        from pathlib import Path
        import signal
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "agent.py"
            agent.write_text(
                """import json,os,sys
from pathlib import Path
Path('pid').write_text(str(os.getpid()))
def send(value):print(json.dumps(dict(jsonrpc='2.0',**value)),flush=True)
for line in sys.stdin:
 m=json.loads(line);method=m.get('method')
 if method=='initialize':send(dict(id=m['id'],result={'protocolVersion':1}))
 elif method=='session/new':send(dict(id=m['id'],result={'sessionId':'signal-session'}))
 elif method=='session/prompt':
  prompt=m['id'];Path('prompt').touch()
 elif method=='session/cancel':
  send(dict(method='session/update',params={'sessionId':'signal-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'partial'}}}))
  send(dict(id=prompt,result={'stopReason':'cancelled'}))
""",
                encoding="utf-8",
            )
            config = root / "config.json"
            config.write_text(
                json.dumps({"command": [sys.executable, str(agent)], "cwd": str(root)})
            )
            command = "import asyncio,sys;from dios_sdk.acp import execute;sys.exit(asyncio.run(execute(sys.argv[1],'work',lambda x:x)))"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                command,
                str(config),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(5):
                    while not (root / "prompt").exists():
                        await asyncio.sleep(0.01)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                frames = [json.loads(x) for x in stdout.splitlines()]
                self.assertEqual(process.returncode, 130)
                self.assertFalse(any(x["type"] == "result" for x in frames))
                self.assertTrue(any(x.get("text") == "partial" for x in frames))
                self.assertIn(b"ACP_CANCEL_ACKNOWLEDGED", stderr)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((root / "pid").read_text()), 0)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def test_rejected_large_frame_cannot_deadlock_child_cleanup(self):
        from dios_sdk.acp import execute
        from pathlib import Path
        import os, sys, tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "flood.py"
            agent.write_text(
                "import sys,os;from pathlib import Path;Path('pid').write_text(str(os.getpid()));sys.stdin.readline();sys.stdout.write('x'*1048576+'\\n');sys.stdout.flush();sys.stdin.read()"
            )
            config = root / "config.json"
            config.write_text(
                json.dumps({"command": [sys.executable, str(agent)], "cwd": str(root)})
            )
            with self.assertRaisesRegex(WorkloadError, "ACP_FRAME_TOO_LARGE"):
                await asyncio.wait_for(execute(config, "test", lambda x: x), 5)
            with self.assertRaises(ProcessLookupError):
                os.kill(int((root / "pid").read_text()), 0)
