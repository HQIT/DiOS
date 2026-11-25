import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dios_sdk import WorkloadError
from dios_sdk.acp import execute

SERVER = """import json,sys
for line in sys.stdin:
 m=json.loads(line);method=m.get('method')
 if 'id' not in m:continue
 if method=='initialize':r={'protocolVersion':1}
 elif method=='session/new':r={'sessionId':'one'}
 else:
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'one','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'{"summary":"model claim"}'}}}}),flush=True)
  r={'stopReason':'end_turn'}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':r}),flush=True)
"""


class AsyncResultTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, outcome):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "server.py"
            server.write_text(SERVER)
            config = root / "config.json"
            config.write_text(
                json.dumps({"command": [sys.executable, str(server)], "cwd": directory})
            )
            events = []
            entered = asyncio.Event()
            release = asyncio.Event()

            async def validate(value):
                entered.set()
                await release.wait()
                if outcome == "denied":
                    raise WorkloadError("EVIDENCE_INSUFFICIENT")
                return dict(value, evidence=["verified:child"])

            with patch("dios_sdk.acp.emit", events.append):
                task = asyncio.create_task(execute(config, "test", validate))
                await asyncio.wait_for(entered.wait(), 3)
                self.assertFalse(any(x["type"] == "result" for x in events))
                if outcome == "cancelled":
                    task.cancel()
                else:
                    release.set()
                if outcome == "denied":
                    with self.assertRaisesRegex(WorkloadError, "EVIDENCE_INSUFFICIENT"):
                        await task
                else:
                    self.assertEqual(await task, 130 if outcome == "cancelled" else 0)
            result = [x for x in events if x["type"] == "result"]
            self.assertEqual(len(result), 1 if outcome == "success" else 0)
            if result:
                self.assertEqual(result[0]["result"]["evidence"], ["verified:child"])

    async def test_result_waits_for_external_evidence(self):
        await self.run_case("success")

    async def test_failed_validation_never_emits_result(self):
        await self.run_case("denied")

    async def test_cancel_during_validation_never_emits_result(self):
        await self.run_case("cancelled")


class ChildEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_claimed_completion_requires_owned_success_and_verified_artifact(
        self,
    ):
        path = (
            Path(__file__).resolve().parents[3]
            / "examples/runtime/acp_child_coordinator.py"
        )
        spec = importlib.util.spec_from_file_location("child_coordinator_example", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        channel = AsyncMock()
        value = {"summary": "model claim", "child_run_id": "child"}
        for state, artifacts, result in [
            ("executing", [], None),
            ("failed", [], None),
            ("succeeded", [], {}),
            ("succeeded", [{"kind": "result", "digest": "a" * 64}], None),
        ]:
            channel.child.return_value = {
                "id": "child",
                "state": state,
                "artifacts": artifacts,
                "result": result,
            }
            with self.assertRaisesRegex(WorkloadError, "EVIDENCE_INSUFFICIENT"):
                await module.verified_child_result(value, channel)
        channel.child.side_effect = WorkloadError("NOT_FOUND")
        with self.assertRaisesRegex(WorkloadError, "NOT_FOUND"):
            await module.verified_child_result(value, channel)
        channel.child.side_effect = None
        channel.child.return_value = {
            "id": "child",
            "state": "succeeded",
            "artifacts": [{"kind": "result", "digest": "a" * 64}],
            "result": {"summary": "verified"},
        }
        result = await module.verified_child_result(value, channel)
        self.assertEqual(result["child_result"]["summary"], "verified")
        self.assertIn("sha256:" + "a" * 64, result["evidence"])
