"""R09: real remote Worker, ACP activity, Docker read timeout and report outage."""

import asyncio
import hashlib
import json
import os
import sys
import time
import unittest

from app.runtime.database import RuntimeErrorCode
from app.runtime.overview import view
from app.runtime.spool import ReportSpool
from app.runtime.work_auth_schema import grants
from app.runtime.worker import read_profile
from runtime_observation_proxy import DockerObservationProxy
from sqlalchemy import select, true
from test_remote_recovery_qualification import RemoteRecoveryQualificationTests
from test_runtime_worker import ROOT

OBSERVATIONS = []


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1" and os.environ.get("DIOS_TEST_ACP_IMAGE"),
    "isolated Docker ACP image required",
)
class ObservationQualificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_timeout_activity_and_report_outage(self):
        f = RemoteRecoveryQualificationTests()
        await f.asyncSetUp()
        await asyncio.sleep(1.1)

        async def cleanup():
            while f._cleanups:
                function, args, kwargs = f._cleanups.pop()
                result = function(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    await result

        self.addAsyncCleanup(cleanup)
        path, _ = f.profiles["account-a"]
        value = json.loads(path.read_text())
        image = await asyncio.to_thread(
            f.adapter.client.images.get, os.environ["DIOS_TEST_ACP_IMAGE"]
        )
        value["launch"].update(
            image=image.id,
            user=f"{os.getuid()}:{os.getgid()}",
            command=[
                "python",
                "-u",
                "/qualification/runtime_acp_fault_fixture.py",
                "bridge",
            ],
            environment={
                "PYTHONPATH": "/app:/qualification",
                "ACP_EXPECTED_SOURCE_SHA256": hashlib.sha256(
                    (ROOT / "backend/app/runtime/acp_bridge.py").read_bytes()
                ).hexdigest(),
            },
            mounts=[
                {
                    "source": str(ROOT / "backend/tests"),
                    "target": "/qualification",
                    "read_only": True,
                },
                {
                    "source": str(f.control),
                    "target": "/run/qualification",
                    "read_only": False,
                },
            ],
        )
        path.write_text(json.dumps(value))
        _, spec = read_profile(path)
        f.profiles["account-a"] = (path, spec)
        policy = json.loads(f.policy.path.read_text())
        row = policy["profiles"]["account-a"]
        row["profile_digest"] = spec.fingerprint()
        for permissions in row["permissions"].values():
            for permission in permissions:
                permission["resource_version"] = spec.fingerprint()
        f.policy.path.write_text(json.dumps(policy))
        proxy = DockerObservationProxy(f.root / "docker.sock")
        self.addAsyncCleanup(asyncio.to_thread, proxy.close)
        sync_blocked = False
        sync_failures = 0
        original = f.server.handle

        async def handle(certificate, expiry, envelope):
            nonlocal sync_failures
            if sync_blocked and envelope["message"]["method"] == "runtime.report":
                sync_failures += 1
                raise RuntimeErrorCode("WORKER_GATEWAY_BUSY")
            return await original(certificate, expiry, envelope)

        f.server.handle = handle
        run = await f.store.submit(
            "observation-timeout",
            {
                "runtime_profile": "account-a",
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "payload": {"mode": "waiting"},
            },
            authorization=f.policy.admission("account-a", spec.fingerprint(), []),
        )
        env = dict(
            os.environ,
            PYTHONPATH=str(ROOT / "backend"),
            DOCKER_HOST="unix://" + str(f.root / "docker.sock"),
        )
        env.pop("DIOS_RUNTIME_DATABASE_URL", None)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.runtime.cli",
            "--scope",
            f.store.scope,
            "remote-worker",
            "--profile",
            str(path),
            "--control-file",
            str(f.client_file),
            "--artifacts",
            str(f.root / "worker-0"),
            "--once",
            "--poll-interval",
            ".1",
            cwd=ROOT / "backend",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        f.children.append(process)

        async def until(predicate, timeout=35):
            async with asyncio.timeout(timeout):
                while True:
                    if process.returncode is not None:
                        out, err = await process.communicate()
                        self.fail(
                            "Worker exited early: "
                            + out.decode()[-1500:]
                            + err.decode()[-3000:]
                        )
                    result = await predicate()
                    if result:
                        return result
                    await asyncio.sleep(0.1)

        async def state(diagnosis):
            overview = await view(f.fixture.db, f.store.scope, true())
            rows = overview["active"]["items"]
            return overview if rows and rows[0]["diagnosis"] == diagnosis else None

        initial = await until(lambda: state("waiting_model"))
        first = (await f.store.get(run["id"]))["attempts"][0]
        await asyncio.sleep(0.6)
        activity_start = int((f.control / "activity").read_text())
        proxy.block.set()
        blocked_at = time.monotonic()

        async def blocked_and_stale():
            if proxy.timeouts < 3:
                return None
            return await state("observation_stale")

        stale = await until(blocked_and_stale)
        self.assertIsNone(stale["active"]["actual_concurrency"])
        current = (await f.store.get(run["id"]))["attempts"][0]
        self.assertEqual(current["id"], first["id"])
        self.assertEqual(current["session_id"], first["session_id"])
        self.assertGreater(
            stale["active"]["items"][0]["heartbeat_at"],
            initial["active"]["items"][0]["heartbeat_at"],
        )
        self.assertGreater(current["lease_until"], time.time())
        activity_stale = int((f.control / "activity").read_text())
        self.assertGreater(activity_stale, activity_start)
        self.assertGreaterEqual(proxy.timeouts, 2)
        self.assertEqual((await f.collect(await f.launch()))["state"], "idle")
        proxy.block.clear()
        recovered = await until(lambda: state("waiting_model"))
        self.assertEqual(recovered["active"]["actual_concurrency"], 1)
        sync_blocked = True
        (f.control / "emit-output").touch()
        spool_path = (
            f.root
            / "worker-0"
            / (
                ".reports-"
                + hashlib.sha256(f.store.scope.encode()).hexdigest()
                + ".sqlite"
            )
        )

        async def pending():
            status = await asyncio.to_thread(ReportSpool.inspect, spool_path)
            return status if status["retained"] and sync_failures else None

        pending_reports = await until(pending)
        sync_view = await state("waiting_model")
        self.assertIsNotNone(sync_view)
        self.assertEqual(sync_view["active"]["actual_concurrency"], 1)
        self.assertIsNone(process.returncode)
        sync_blocked = False
        streaming = await until(lambda: state("streaming"))
        (f.control / "release").touch()
        result = await f.collect(process)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(result["attempts"]), 1)
        last = result["attempts"][0]
        self.assertEqual(
            (last["id"], last["session_id"], last["epoch"]),
            (first["id"], first["session_id"], first["epoch"]),
        )
        prompts = [
            json.loads(line)
            for line in (f.control / "prompts.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["session"], first["session_id"])
        self.assertEqual(
            (await asyncio.to_thread(ReportSpool.inspect, spool_path))["retained"], 0
        )
        async with f.fixture.db.read(f.store.scope) as connection:
            grant_rows = (
                (
                    await connection.execute(
                        select(grants).where(grants.c.run_id == run["id"])
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual(len(grant_rows), 1)
        OBSERVATIONS.append(
            {
                "case": "R09",
                "image": image.id,
                "worker_pid": process.pid,
                "run_id": run["id"],
                "attempt_id": first["id"],
                "session_id": first["session_id"],
                "process_id": first["process_id"],
                "process_instance": first["process_instance"],
                "initial": initial,
                "stale": stale,
                "recovered": recovered,
                "sync_outage": sync_view,
                "sync_recovered": streaming,
                "pending_reports": pending_reports,
                "docker_timeouts": proxy.timeouts,
                "query_outage_seconds": time.monotonic() - blocked_at,
                "sync_failures": sync_failures,
                "activity_before": activity_start,
                "activity_during_outage": activity_stale,
                "renewed_lease_until": current["lease_until"],
                "prompt_calls": len(prompts),
                "attempts": len(result["attempts"]),
                "e2ag_grants": len(grant_rows),
                "final_state": result["state"],
            }
        )
