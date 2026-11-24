"""Native suspension never creates a new task, session, approval or execution grant."""

import asyncio
import os
import unittest
from dataclasses import replace
from uuid import uuid4

import test_runtime_work_authorizations as auth
from app.runtime.adapter import LaunchSpec
from app.runtime.database import RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.identity_wait import identity_wait
from app.runtime.schema import attempts
from app.runtime.work_auth_schema import work
from app.runtime.work_authorizations import view
from test_runtime_worker import FakeAdapter, protocol


class Pausable(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.paused = False
        self.resumed = 0
        self.pause_seen = asyncio.Event()

    def observation(self, handle):
        value = super().observation(handle)
        return replace(value, state="paused") if self.paused and self.running else value

    async def observe(self, handle):
        if self.resumed:
            self.running = False
        return self.observation(handle)

    async def logs(self, handle, max_bytes):
        return replace(
            await super().logs(handle, max_bytes),
            stdout=protocol(complete=bool(self.resumed)),
        )

    async def pause(self, handle, process_instance):
        self.paused = True
        self.pause_seen.set()
        return self.observation(handle)

    async def resume(self, handle, process_instance):
        self.paused = False
        self.resumed += 1
        return self.observation(handle)


class IdentityPauseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = auth.WorkAuthorizationTests.asyncSetUp
    asyncTearDown = auth.WorkAuthorizationTests.asyncTearDown
    submit = auth.WorkAuthorizationTests.submit
    worker = auth.WorkAuthorizationTests.worker

    async def launch(self, adapter=None):
        run = await self.submit()
        adapter = adapter or Pausable()
        worker = self.worker(adapter)
        recovered = asyncio.Event()

        async def verify(context, action):
            if context.session_id and not recovered.is_set():
                raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")
            return await self.verifier(context, action)

        worker.authorization_verifier = verify
        task = asyncio.create_task(worker.once())
        self.addAsyncCleanup(self.cleanup_task, task)
        return run, adapter, recovered, task

    async def cleanup_task(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def paused_record(self, run_id):
        for _ in range(100):
            run = await self.store.get(run_id)
            if (
                run["attempts"]
                and run["attempts"][0]["remote"].get("runtime", {}).get("state")
                == "paused"
            ):
                return run
            await asyncio.sleep(0.01)
        self.fail("Pause evidence was not persisted")

    async def test_pause_keeps_attempt_and_grant_then_resumes_original_session(self):
        run, adapter, recovered, task = await self.launch()
        await asyncio.wait_for(adapter.pause_seen.wait(), 3)
        await asyncio.sleep(0.05)
        before = await self.store.get(run["id"])
        authority = await view(self.store, run["id"])
        status = await self.store.runtime_status()
        self.assertEqual(status["confirmed_paused_processes"], 1)
        self.assertEqual(status["actual_concurrency"], 0)
        self.assertFalse(adapter.stopped)
        recovered.set()
        result = await asyncio.wait_for(task, 4)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempt_id"], before["attempt_id"])
        self.assertEqual(result["attempts"][0]["session_id"], "fixture-session")
        self.assertEqual(
            (await view(self.store, run["id"]))["reference"], authority["reference"]
        )
        self.assertEqual((adapter.started, adapter.resumed), (1, 1))

    async def test_revocation_while_paused_never_unfreezes(self):
        run, adapter, recovered, task = await self.launch()
        await asyncio.wait_for(adapter.pause_seen.wait(), 3)
        current = await self.paused_record(run["id"])
        await self.store.revoke(run["id"], current["version"])
        recovered.set()
        result = await asyncio.wait_for(task, 4)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(adapter.resumed, 0)
        self.assertTrue(adapter.stopped)
        self.assertEqual((await view(self.store, run["id"]))["state"], "revoked")

    async def test_expired_work_while_paused_never_unfreezes(self):
        run, adapter, recovered, task = await self.launch()
        await asyncio.wait_for(adapter.pause_seen.wait(), 3)
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                work.update().where(work.c.run_id == run["id"]).values(expires_at=0)
            )
        recovered.set()
        result = await asyncio.wait_for(task, 4)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(adapter.resumed, 0)
        self.assertTrue(adapter.stopped)

    async def test_cancel_while_paused_confirms_exit_without_resuming(self):
        run, adapter, recovered, task = await self.launch()
        current = await self.paused_record(run["id"])
        await self.store.cancel(run["id"], current["version"])
        recovered.set()
        result = await asyncio.wait_for(task, 4)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(adapter.resumed, 0)
        self.assertTrue(adapter.stopped)

    async def test_unsupported_adapter_stops_without_replay(self):
        adapter = Pausable()
        adapter.pause = None
        adapter.resume = None
        _run, adapter, _, task = await self.launch(adapter)
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(adapter.started, 1)
        self.assertTrue(adapter.stopped)

    async def test_expired_owner_cannot_resume_frozen_process(self):
        run, adapter, recovered, task = await self.launch()
        current = await self.paused_record(run["id"])
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == current["attempt_id"])
                .values(lease_until=0)
            )
        await self.store.expire()
        recovered.set()
        with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
            await asyncio.wait_for(task, 4)
        self.assertEqual(adapter.resumed, 0)
        self.assertEqual(
            (await self.store.get(run["id"]))["state"], "reconciliation_required"
        )

    async def test_wait_timeout_leaves_frozen_process_for_safe_stop(self):
        calls = []

        async def check():
            raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")

        async def pause():
            calls.append("pause")

        async def resume():
            calls.append("resume")

        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PROVIDER_UNAVAILABLE"):
            await identity_wait(check, suspend=pause, resume=resume, timeout=0.02)
        self.assertEqual(calls, ["pause"])

    async def test_permanent_denial_is_never_retried(self):
        calls = []

        async def check():
            calls.append("check")
            raise RuntimeErrorCode("AUTH_REVOKED")

        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await identity_wait(check)
        self.assertEqual(calls, ["check"])


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1", "native Docker qualification not enabled"
)
class DockerFreezeTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_freeze_preserves_instance_and_kill_never_runs_more_code(self):
        adapter = DockerRuntimeAdapter("freeze-test-" + str(uuid4()))
        handle = str(uuid4())
        spec = LaunchSpec(
            "dios-worker-fixture:local",
            (
                "python",
                "-u",
                "-c",
                "import time\ni=0\nwhile True:\n print(i,flush=True)\n i+=1\n time.sleep(.05)",
            ),
        )
        try:
            first = await adapter.start(handle, spec)
            await asyncio.sleep(0.25)
            paused = await adapter.pause(handle, first.process_instance)
            self.assertEqual(paused.state, "paused")
            self.assertTrue(paused.alive)
            self.assertEqual(paused.pid, first.pid)
            await asyncio.sleep(0.2)  # Docker log delivery can lag process freezing.
            before = await adapter.logs(handle, 65536)
            await asyncio.sleep(0.2)
            self.assertEqual((await adapter.logs(handle, 65536)).stdout, before.stdout)
            with self.assertRaisesRegex(RuntimeErrorCode, "PROCESS_CHANGED"):
                await adapter.resume(handle, "wrong-instance")
            resumed = await adapter.resume(handle, first.process_instance)
            self.assertEqual(resumed.process_instance, first.process_instance)
            await asyncio.sleep(0.2)
            self.assertNotEqual(
                (await adapter.logs(handle, 65536)).stdout, before.stdout
            )
            await adapter.pause(handle, first.process_instance)
            await asyncio.sleep(0.2)
            before = await adapter.logs(handle, 65536)
            exited = await adapter.stop(handle)
            self.assertFalse(exited.alive)
            self.assertEqual(exited.exit_code, 137)
            self.assertEqual((await adapter.logs(handle, 65536)).stdout, before.stdout)
        finally:
            await adapter.stop(handle, 0)
            await adapter.remove(handle)
            adapter.client.close()
