"""R04/R07 through governed Workers, Docker processes and the production ACP bridge."""

import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path

import test_runtime_worker as fixtures
from app.runtime.adapter import Mount
from app.runtime.bridge import JsonlBridge
from app.runtime.reconciliation import ExitReceipts
from app.runtime.schema import artifacts, attempts
from app.runtime.work_auth_schema import grants
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker
from sqlalchemy import select
from test_reference_faults import review_input
from work_auth_fixtures import policy_fixture

ROOT = Path(__file__).resolve().parents[2]
OBSERVATIONS = []


class NativeAcpCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_monitor_reaps_inherited_pipe_descendant(self):
        from app.runtime.acp_bridge import AcpClient
        from app.runtime.database import RuntimeErrorCode

        with tempfile.TemporaryDirectory() as directory:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "backend/tests/runtime_acp_fault_fixture.py"),
                "server",
                env=dict(
                    os.environ,
                    ACP_FAULT_MODE="crash",
                    ACP_FAULT_JOURNAL=str(Path(directory) / "calls"),
                ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            peer = AcpClient(
                process, emit_event=lambda event: None, owns_process_group=True
            )
            peer.reader = asyncio.create_task(peer.read())
            peer.stderr = asyncio.create_task(peer.discard_stderr())
            peer.monitor = asyncio.create_task(peer.watch_process())
            try:
                await peer.call("initialize", {})
                peer.session_id = (await peer.call("session/new", {}))["sessionId"]
                with self.assertRaisesRegex(RuntimeErrorCode, "ACP_PROCESS_EXITED"):
                    await asyncio.wait_for(
                        peer.call("session/prompt", {}, timeout=20), 2
                    )
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assertIn("部分输出🙂", peer.output)
                descendant = int(peer.output.rsplit("=", 1)[1])
            finally:
                await asyncio.wait_for(peer.close(), 3)
            # A reparented zombie is exited; no descendant may continue holding a pipe.
            stat = Path(f"/proc/{descendant}/stat")
            for _ in range(100):
                if (
                    not stat.exists()
                    or stat.read_text().split(")", 1)[1].split()[0] == "Z"
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("ACP descendant survived private process-group cleanup")
            self.assertFalse(peer.pending)
            self.assertTrue(peer.monitor.done())


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1" and os.environ.get("DIOS_TEST_ACP_IMAGE"),
    "isolated ACP image required",
)
class NativeAcpReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.DockerWorkerTests()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.journal = self.f.root / "journal"
        self.journal.mkdir(mode=0o777)
        self.journal.chmod(0o777)
        self.f.spec = replace(
            self.f.spec,
            image=os.environ["DIOS_TEST_ACP_IMAGE"],
            command=("python", "/qualification/runtime_acp_fault_fixture.py", "bridge"),
            environment={
                "PYTHONPATH": "/app",
                "PYTHONDONTWRITEBYTECODE": "1",
                "ACP_EXPECTED_SOURCE_SHA256": hashlib.sha256(
                    (ROOT / "backend/app/runtime/acp_bridge.py").read_bytes()
                ).hexdigest(),
            },
            memory_bytes=256 * 1024 * 1024,
            mounts=(
                Mount(str(ROOT / "backend/tests"), "/qualification"),
                Mount(str(self.journal), "/run/qualification", False),
            ),
        )
        self.policy, self.authorization = policy_fixture(
            self.f.root, "test-profile", self.f.spec
        )

    async def submit(self, key, mode):
        return await self.f.store.submit(
            key,
            {
                "runtime_profile": "test-profile",
                "profile_digest": self.f.spec.fingerprint(),
                "secret_refs": [],
                "payload": {"mode": mode, "reference": review_input()},
            },
            authorization=self.authorization,
        )

    def worker(self):
        return Worker(
            self.f.store,
            self.f.adapter,
            "test-profile",
            self.f.spec,
            self.f.provider,
            poll_interval=0.05,
            timeout=40,
            authorization_verifier=WorkAuthorizationVerifier(self.f.store, self.policy),
        )

    def prompts(self):
        return [
            json.loads(line)
            for line in (self.journal / "prompts.jsonl").read_text().splitlines()
        ]

    async def retained(self, run_id):
        async with self.f.db.read(self.f.scope) as c:
            rows = (
                (await c.execute(select(artifacts).where(artifacts.c.run_id == run_id)))
                .mappings()
                .all()
            )
        return b"\n".join(
            b"".join(self.f.provider.read(row["storage_key"])) for row in rows
        )

    async def grant_rows(self, run_id):
        async with self.f.db.read(self.f.scope) as c:
            return [
                dict(row)
                for row in (
                    await c.execute(select(grants).where(grants.c.run_id == run_id))
                ).mappings()
            ]

    async def test_pending_rpc_sigkill_retains_output_and_same_worker_continues(self):
        failed = await self.submit("crash", "crash")
        following = await self.submit("next", "success")
        worker = self.worker()
        started = time.monotonic()
        result = await asyncio.wait_for(worker.once(), 12)
        elapsed = time.monotonic() - started
        self.assertEqual(result["id"], failed["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["attempts"][0]["error"]["code"], "ACP_PROCESS_EXITED")
        raw = await self.retained(failed["id"])
        self.assertIn("部分输出🙂".encode(), raw)
        self.assertIn(b"ACP_PROCESS_EXITED", raw)
        self.assertEqual(len(result["attempts"]), 1)
        self.assertFalse(result["attempts"][0]["remote"]["runtime"]["alive"])
        next_result = await asyncio.wait_for(worker.once(), 12)
        self.assertEqual(next_result["id"], following["id"])
        self.assertEqual(next_result["state"], "succeeded")
        before, after = result["attempts"][0], next_result["attempts"][0]
        self.assertEqual(before["worker_id"], after["worker_id"])
        self.assertEqual(before["worker_instance"], after["worker_instance"])
        self.assertNotEqual(before["session_id"], after["session_id"])
        self.assertEqual(len(self.prompts()), 2)
        for run in (result, next_result):
            self.assertTrue(await self.grant_rows(run["id"]))
        OBSERVATIONS.append(
            {
                "case": "R04",
                "seconds": elapsed,
                "input_digest": hashlib.sha256(
                    json.dumps(review_input(), sort_keys=True).encode()
                ).hexdigest(),
                "worker_id": before["worker_id"],
                "runs": [failed["id"], following["id"]],
                "attempts": [
                    {
                        key: row[key]
                        for key in ("id", "epoch", "session_id", "process_instance")
                    }
                    for row in (before, after)
                ],
                "error": before["error"],
                "states": [result["state"], next_result["state"]],
                "prompt_calls": len(self.prompts()),
                "e2ag_grants_verified": True,
            }
        )

    async def test_worker_sigkill_after_durable_result_recovers_without_prompt_replay(
        self,
    ):
        run = await self.submit("durable-result", "success")
        profile = self.f.root / "profile.json"
        profile.write_text(
            json.dumps(
                {
                    "id": "test-profile",
                    "bridge": "jsonl-v1",
                    "launch": asdict(self.f.spec),
                }
            )
        )
        config = self.f.root / "child.json"
        config.write_text(
            json.dumps(
                {
                    "url": self.f.url,
                    "scope": self.f.scope,
                    "profile": str(profile),
                    "policy": str(self.f.root / "work-policy.json"),
                    "artifacts": str(self.f.provider.root),
                }
            )
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "backend/tests/runtime_acp_fault_fixture.py"),
            "worker",
            str(config),
            env=dict(os.environ, PYTHONPATH=str(ROOT / "backend")),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.f.children.append(child)
        _, err = await asyncio.wait_for(child.communicate(), 15)
        self.assertEqual(child.returncode, -signal.SIGKILL, err)
        interrupted = await self.f.store.get(run["id"])
        self.assertNotIn(interrupted["state"], {"succeeded", "failed"})
        attempt = interrupted["attempts"][0]
        receipts = ExitReceipts(self.f.provider, self.f.scope)
        receipt = receipts.load(attempt)
        self.assertIsNotNone(receipt)
        bridge = JsonlBridge()
        bridge.feed(b"".join(self.f.provider.read(receipt["stdout"])), final=True)
        self.assertIsNotNone(bridge.result)
        self.assertEqual(len(self.prompts()), 1)
        async with self.f.db.transaction(self.f.scope) as c:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(lease_until=0)
            )
        await self.f.store.expire()
        recovered_by = self.worker()
        await recovered_by.recover()
        recovered = await self.f.store.get(run["id"])
        self.assertEqual(recovered["state"], "succeeded")
        self.assertEqual(len(recovered["attempts"]), 1)
        self.assertEqual(recovered["attempts"][0]["id"], attempt["id"])
        self.assertEqual(recovered["attempts"][0]["session_id"], bridge.session_id)
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual((await recovered_by.once())["state"], "idle")
        gs = await self.grant_rows(run["id"])
        self.assertTrue(gs)
        self.assertTrue(all(g["attempt_id"] == attempt["id"] for g in gs))
        OBSERVATIONS.append(
            {
                "case": "R07",
                "run_id": run["id"],
                "attempt_id": attempt["id"],
                "input_digest": hashlib.sha256(
                    json.dumps(review_input(), sort_keys=True).encode()
                ).hexdigest(),
                "epoch": attempt["epoch"],
                "session_id": bridge.session_id,
                "worker_exit": child.returncode,
                "receipt_stdout": receipt["stdout"],
                "prompt_calls_before": 1,
                "prompt_calls_after": len(self.prompts()),
                "state": recovered["state"],
                "e2ag_grants_verified": True,
            }
        )
