"""R10/R11 over native Workers, real mTLS and independent progress/content requests."""

import asyncio
import copy
import json
import os
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

import test_remote_recovery_qualification as recovery_fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.remote_runtime import RemoteRuntimeStore
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.schema import artifacts
from app.runtime.worker import read_profile
from app.services.run_evidence import RunEvidence
from app.services.task_console import TaskConsole
from sqlalchemy import select
from work_auth_fixtures import policy_fixture

OBSERVATIONS = []


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1", "isolated Docker required"
)
class NativeRemoteConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = recovery_fixtures.RemoteRecoveryQualificationTests()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.fixture.asyncTearDown)
        self.addAsyncCleanup(self.f.cleanup_native)
        self.console = TaskConsole(
            self.f.fixture.db,
            self.f.store.scope,
            self.f.root,
            self.f.fixture.provider.root,
            policy=self.f.policy,
        )
        self.viewer = RunEvidence(self.console)
        self.messages = []
        self.original = self.f.server.handle

        async def capture(cert, expiry, envelope):
            self.messages.append(copy.deepcopy(envelope))
            return await self.original(cert, expiry, envelope)

        self.f.server.handle = capture

    def transport(self, profile="account-a"):
        return RemoteToolProvider(
            self.f.client_file,
            SimpleNamespace(scope=self.f.store.scope),
            profile,
            self.f.profiles[profile][1],
        )

    async def wait_run(self, run_id, predicate):
        async with asyncio.timeout(25):
            while True:
                run = await self.f.store.get(run_id)
                if predicate(run):
                    return run
                await asyncio.sleep(0.05)

    async def test_old_epoch_payloads_are_quarantined_once_without_overwriting_new_attempt(
        self,
    ):
        (self.f.control / "outage").write_text("PROCESS_EXITED")
        run = await self.f.submit("old-owner")
        first = await self.f.collect(await self.f.launch())
        self.assertEqual(first["state"], "retry_wait")
        old_attempt = first["attempts"][0]
        self.assertEqual(old_attempt["epoch"], 1)
        original_report = next(
            e for e in self.messages if e["message"]["method"] == "runtime.report"
        )
        old_key = next(
            e["message"]["params"]["key"]
            for e in self.messages
            if e["message"]["method"] == "runtime.retain"
        )
        (self.f.control / "outage").unlink()
        await asyncio.sleep(1.1)
        next_worker = await self.f.launch()
        current = await self.wait_run(
            run["id"],
            lambda r: r["epoch"] == 2 and r["attempts"][-1]["state"] == "streaming",
        )
        new_attempt = next(a for a in current["attempts"] if a["epoch"] == 2)
        self.assertNotEqual(new_attempt["worker_id"], old_attempt["worker_id"])
        sent = copy.deepcopy(original_report)
        sent["message"]["params"]["report"] = {
            "sequence": 99,
            "phase": "streaming",
            "session_id": old_attempt["session_id"],
            "output": True,
            "output_observed_at": time.time(),
            "output_excerpt": "旧所有者迟到输出🙂",
        }
        transport = self.transport()
        for _ in range(10):
            sent["request_id"] = uuid4().hex
            with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
                await transport.exchange(sent)
        completed = copy.deepcopy(sent)
        completed["message"] = {
            "method": "runtime.complete",
            "params": {
                "binding": original_report["message"]["params"]["binding"],
                "key": old_key,
            },
        }
        with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
            await transport.exchange(completed)
        after = await self.f.store.get(run["id"])
        for key in ("epoch", "attempt_id", "state", "version"):
            self.assertEqual(after[key], current[key])
        page = await self.viewer.view(run["id"], section="quarantine", limit=1)
        self.assertIsNotNone(page["next_cursor"])
        rest = await self.viewer.view(
            run["id"], section="quarantine", limit=1, cursor=page["next_cursor"]
        )
        isolated = page["items"] + rest["items"]
        self.assertEqual(len(isolated), 2)
        self.assertTrue(
            all(
                r["attempt_id"] == old_attempt["id"] and r["epoch"] == 1
                for r in isolated
            )
        )
        report = next(r for r in isolated if r["values"]["method"] == "report")
        self.assertEqual(report["values"]["current_epoch"], 2)
        self.assertEqual(
            report["values"]["payload"]["output_excerpt"], "旧所有者迟到输出🙂"
        )
        self.assertFalse(report["values"]["accepted_as_result"])
        # Same machine certificate with another process identity cannot impersonate the old sender.
        another = self.transport()
        remote = RemoteRuntimeStore(self.f.store.scope, another)
        await remote.register_worker(remote.worker_id, uuid4().hex, "test", os.getpid())
        forged = copy.deepcopy(sent)
        forged.update(
            worker_id=remote.worker_id, instance=remote.instance, request_id=uuid4().hex
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
            await another.exchange(forged)
        self.assertEqual(
            len((await self.viewer.view(run["id"], section="quarantine"))["items"]), 2
        )
        (self.f.control / "release").touch()
        await self.f.collect(next_worker)
        final = await self.f.store.get(run["id"])
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(len(final["attempts"]), 2)
        OBSERVATIONS.append(
            {
                "case": "R10",
                "run_id": run["id"],
                "old_attempt": old_attempt["id"],
                "current_attempt": new_attempt["id"],
                "epochs": [1, 2],
                "replays": 10,
                "quarantined_records": len(isolated),
                "quarantine_page": await self.viewer.view(
                    run["id"], section="quarantine"
                ),
                "run_unchanged_on_late_write": True,
                "distinct_worker_ids": [
                    old_attempt["worker_id"],
                    new_attempt["worker_id"],
                ],
                "forged_sender_rejected": True,
                "final_state": final["state"],
            }
        )

    async def test_detail_outage_preserves_independent_progress_watermark_and_resumes(
        self,
    ):
        profile, _ = self.f.profiles["account-a"]
        value = json.loads(profile.read_text())
        script = """import json,os,time,uuid,pathlib
sid=str(uuid.uuid4())
def emit(v): print(json.dumps(v,ensure_ascii=False),flush=True)
emit({'type':'session','session_id':sid})
for n in range(80):emit({'type':'output','text':'中文进度🙂'*150})
while not pathlib.Path('/run/fixture/release').exists():time.sleep(.05)
emit({'type':'result','session_id':sid,'result':{'summary':'完整报告','evidence':['fixture:source']}})
"""
        value["launch"]["command"] = ["python", "-u", "-c", script]
        profile.write_text(json.dumps(value))
        _, spec = read_profile(profile)
        self.f.profiles["account-a"] = (profile, spec)
        policy, _ = policy_fixture(self.f.root, "account-a", spec)
        self.f.policy = policy
        self.f.server.policy = policy
        self.console.policy = policy
        run = await self.f.submit("detail-outage")
        blocked, release = asyncio.Event(), asyncio.Event()
        rejected = False
        resumes = []

        async def filter_upload(cert, expiry, envelope):
            nonlocal rejected
            self.messages.append(copy.deepcopy(envelope))
            method, params = (
                envelope["message"]["method"],
                envelope["message"]["params"],
            )
            if (
                method == "runtime.upload.chunk"
                and params["offset"] == 65536
                and not rejected
            ):
                rejected = True
                blocked.set()
                await release.wait()
                raise RuntimeErrorCode("WORKER_GATEWAY_UNAVAILABLE")
            result = await self.original(cert, expiry, envelope)
            if method == "runtime.upload.begin":
                resumes.append(result["received"])
            return result

        self.f.server.handle = filter_upload
        worker = await self.f.launch()
        await self.wait_run(
            run["id"],
            lambda r: r["attempts"] and r["attempts"][0]["state"] == "streaming",
        )
        (self.f.control / "release").touch()
        await asyncio.wait_for(blocked.wait(), 20)
        current = await self.f.store.get(run["id"])
        attempt = current["attempts"][0]
        self.assertEqual(attempt["state"], "validating")
        progress = next(
            e
            for e in reversed(self.messages)
            if e["message"]["method"] == "runtime.report"
        )
        sequence = progress["message"]["params"]["report"]["sequence"]
        transport = self.transport()
        newer = copy.deepcopy(progress)
        newer["message"]["params"]["report"].update(
            sequence=sequence + 2,
            output=True,
            output_excerpt="详情待同步，小进度继续",
            output_observed_at=time.time(),
        )
        older = copy.deepcopy(newer)
        older["message"]["params"]["report"].update(
            sequence=sequence + 1, output_excerpt="旧进度", output_observed_at=1
        )
        for envelope in (newer, older, newer):
            envelope["request_id"] = uuid4().hex
            await transport.exchange(envelope)
        page = await self.viewer.view(run["id"], section="transfers")
        pending = next(
            row for row in page["items"] if row["values"]["total_bytes"] > 65536
        )
        self.assertEqual(pending["values"]["received_bytes"], 65536)
        self.assertEqual(pending["values"]["progress_sequence"], sequence + 2)
        self.assertFalse(pending["values"]["content_verified"])
        self.assertIsNone(pending["values"]["last_chunk_at"])
        stored = await self.f.store.get(run["id"])
        self.assertEqual(
            stored["attempts"][0]["remote"]["progress"]["text"],
            "详情待同步，小进度继续",
        )
        release.set()
        _, err = await asyncio.wait_for(worker.communicate(), 20)
        self.assertNotEqual(worker.returncode, 0, err)
        recovered_by = await self.f.launch()
        await self.f.collect(recovered_by)
        final = await self.f.store.get(run["id"])
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(len(final["attempts"]), 1)
        self.assertEqual(final["attempt_id"], attempt["id"])
        self.assertIn(65536, resumes)
        complete = (await self.viewer.view(run["id"], section="transfers"))["items"]
        self.assertTrue(all(x["values"]["content_verified"] for x in complete))
        async with self.f.fixture.db.read(self.f.store.scope) as c:
            linked = (
                (
                    await c.execute(
                        select(artifacts).where(artifacts.c.run_id == run["id"])
                    )
                )
                .mappings()
                .all()
            )
        for row in linked:
            self.f.fixture.provider.verify(row["storage_key"])
        OBSERVATIONS.append(
            {
                "case": "R11",
                "run_id": run["id"],
                "attempt_id": attempt["id"],
                "epoch": attempt["epoch"],
                "session_id": attempt["session_id"],
                "process_instance": attempt["process_instance"],
                "pending_detail": pending,
                "transfer_page": page,
                "resumed_offsets": resumes,
                "artifacts_verified": len(linked),
                "attempts_after_recovery": 1,
                "final_state": final["state"],
            }
        )


class QuarantineValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_identity_cannot_read_operational_payloads(self):
        from test_runtime_remote_control import RemoteControlTests

        f = RemoteControlTests()
        await f.asyncSetUp()
        self.addAsyncCleanup(f.f.asyncTearDown)
        console = TaskConsole(f.f.db, f.f.scope, f.f.root, f.f.provider.root)
        console.principal = {"id": "application"}
        console.access_enforced = (
            False  # isolate section-level gate from run authorization
        )
        for section in ("quarantine", "transfers"):
            page = await RunEvidence(console).view(f.run["id"], section=section)
            self.assertEqual(page["access"], "restricted")
            self.assertEqual(page["items"], [])
            self.assertIsNone(page["next_cursor"])
