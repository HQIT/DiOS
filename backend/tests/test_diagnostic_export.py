"""Bounded exports preserve useful evidence without raw content or authority."""

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile
from sqlalchemy import select
from app.runtime import diagnostic_export as export
from app.runtime.schema import attempts, events, runs, projections
from app.runtime.monitor import LoopObservation
from app.runtime.database import RuntimeErrorCode
from app.runtime.objects import reference
from app.services.task_console import TaskConsole
import test_run_evidence as fixtures


def unpack(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert set(z.namelist()) == {"manifest.json", "diagnostic.json", "README.md"}
        assert sum(i.file_size for i in z.infolist()) <= export.MAX_BYTES
        manifest = json.loads(z.read("manifest.json"))
        for name, info in manifest["files"].items():
            raw = z.read(name)
            assert info == dict(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        return json.loads(z.read("diagnostic.json")), manifest


class ExportScenarios:
    postgres = False
    asyncSetUp = fixtures.EvidenceScenarios.asyncSetUp
    asyncTearDown = fixtures.EvidenceScenarios.asyncTearDown
    artifact = fixtures.EvidenceScenarios.artifact

    async def bundle(self, **kw):
        f = self.f
        async with f.db.read(f.scope) as c:
            run = await f.store._run(c, f.run["id"])
            return await export.build(
                c,
                f.scope,
                run,
                await f.db.now(c),
                include_tools=True,
                include_artifacts=True,
                **kw,
            )

    async def test_secrets_unicode_and_package_local_links(self):
        f = self.f
        sentinel = "客户机密🙂Bearer-password-超长秘密"
        await f.request(arguments={"query": sentinel})
        await self.artifact(sentinel.encode() * 5000)
        async with f.db.transaction(f.scope) as c:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == f.attempt["id"])
                .values(
                    error={"code": sentinel, "message": sentinel},
                    remote={
                        "progress": {"text": sentinel},
                        "runtime": {"secret": sentinel},
                    },
                )
            )
        blob = await self.bundle()
        data, manifest = unpack(blob)
        text = json.dumps(data, ensure_ascii=False)
        for value in [sentinel, "session-one", "operator:test", str(f.provider.root)]:
            self.assertNotIn(value, text)
        self.assertEqual(data["tools"][0]["state"], "awaiting_approval")
        self.assertEqual(data["tools"][0]["attempt"], data["attempts"][0]["attempt"])
        self.assertEqual(
            data["artifacts"][0]["attempt"], data["attempts"][0]["attempt"]
        )
        self.assertEqual(data["grants"][0]["session"], data["attempts"][0]["session"])
        self.assertNotEqual(
            data["attempts"][0]["attempt"],
            unpack(await self.bundle())[0]["attempts"][0]["attempt"],
        )
        self.assertLess(len(blob), export.MAX_BYTES)
        self.assertEqual(manifest["run_id"], f.run["id"])
        self.assertEqual(
            data["observability"]["agent_internal_activity"], "not_observed"
        )

    async def test_bounds_and_byte_truncation_are_explicit(self):
        for n in range(5):
            await self.artifact(str(n).encode())
        with patch.dict(export.LIMITS, {"artifacts": 2}):
            data, manifest = unpack(await self.bundle())
        self.assertEqual(len(data["artifacts"]), 2)
        self.assertTrue(manifest["coverage"]["artifacts"]["has_more"])
        # A lower test byte cap exercises actual trimming without thousands of records.
        with patch.object(export, "MAX_BYTES", 35000):
            data, manifest = unpack(await self.bundle())
            self.assertTrue(
                any(v.get("size_trimmed") for v in manifest["coverage"].values())
            )

    async def test_model_wait_scheduler_stale_and_receiver_lag_are_independent(self):
        f = self.f
        monitor = LoopObservation(f.db, f.scope, "run_scheduler")

        async def ok():
            return None

        await monitor.cycle(ok)
        first = (await f.store.pending_events("dashboard", 100))[0]
        await f.store.apply_projection("dashboard", first)
        from app.runtime.monitor_schema import loops

        async with f.db.transaction(f.scope) as c:
            now = await f.db.now(c)
            await c.execute(
                loops.update()
                .where(loops.c.scope == f.scope)
                .values(heartbeat_at=now - 120)
            )
            await c.execute(
                attempts.update()
                .where(attempts.c.id == f.attempt["id"])
                .values(
                    started_at=now - 1200,
                    last_output_at=now - 900,
                    lease_until=now + 60,
                    remote={
                        "runtime": {
                            "observed_at": now,
                            "alive": True,
                            "pid": 123,
                            "process_instance": "one",
                            "state": "running",
                        }
                    },
                )
            )
        data, _ = unpack(await self.bundle())
        self.assertEqual(data["attempts"][0]["state"], "waiting_model")
        self.assertTrue(data["attempts"][0]["confirmed_running"])
        self.assertGreaterEqual(data["attempts"][0]["seconds_since_output"], 900)
        self.assertEqual(data["schedulers"][0]["diagnosis"], "stale")
        self.assertEqual(data["sync"][0]["state"], "behind")
        self.assertGreater(data["sync"][0]["lag_versions"], 0)
        latest = (await f.store.pending_events("dashboard", 100))[-1]
        await f.store.apply_projection("dashboard", latest)
        self.assertEqual(unpack(await self.bundle())[0]["sync"][0]["state"], "current")
        before = await f.store.get(f.run["id"])
        await self.bundle()
        self.assertEqual((await f.store.get(f.run["id"]))["version"], before["version"])

    async def test_remote_observation_and_missing_observation_are_not_process_evidence(
        self,
    ):
        f = self.f
        data, _ = unpack(await self.bundle())
        self.assertFalse(data["attempts"][0]["confirmed_running"])
        async with f.db.transaction(f.scope) as c:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == f.attempt["id"])
                .values(
                    remote={
                        "a2a": {
                            "observed_at": await f.db.now(c),
                            "observed_state": "working",
                        }
                    }
                )
            )
        data, _ = unpack(await self.bundle())
        self.assertEqual(data["attempts"][0]["evidence_level"], "remote_reported")
        self.assertFalse(data["attempts"][0]["confirmed_running"])

    async def test_service_read_transactions_and_object_checks(self):
        await self.artifact()
        with patch("app.services.task_console.is_console_input", return_value=True):
            data, _ = unpack(await self.console.diagnostic_export(self.f.run["id"]))
        self.assertTrue(data["artifacts"])
        self.assertIsNotNone(data["attempts"][0]["session"])

    async def test_cli_new_file_and_no_overwrite(self):
        import sys

        target = self.f.root / "诊断.zip"
        env = dict(os.environ, DIOS_RUNTIME_DATABASE_URL=self.f.url)
        args = [
            sys.executable,
            "-m",
            "app.runtime.cli",
            "--scope",
            self.f.scope,
            "diagnostic-export",
            self.f.run["id"],
            "--output",
            str(target),
        ]
        p = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await p.communicate()
        self.assertEqual(p.returncode, 0, err.decode())
        unpack(target.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        before = target.read_bytes()
        p = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await p.communicate()
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(target.read_bytes(), before)


class SQLiteExportTests(ExportScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "Isolated PostgreSQL required"
)
class PostgresExportTests(ExportScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class ExportAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.EvidenceAccessTests.asyncSetUp
    asyncTearDown = fixtures.EvidenceAccessTests.asyncTearDown
    save = fixtures.EvidenceAccessTests.save
    headers_for = fixtures.EvidenceAccessTests.headers_for
    submit_as = fixtures.EvidenceAccessTests.submit_as
    get = fixtures.EvidenceAccessTests.get
    finish = fixtures.EvidenceAccessTests.finish
    set_policy = fixtures.EvidenceAccessTests.set_policy
    path = fixtures.EvidenceAccessTests.path

    async def test_run_read_object_permissions_and_operational_visibility(self):
        run = await self.submit_as("a")
        await self.finish()
        detail = await self.console.detail(run["id"])
        art = detail["artifacts"][0]["object_ref"]
        session = detail["attempts"][0]["object_ref"]
        with patch.object(
            self.console.provider,
            "read",
            side_effect=AssertionError("No artifact content read"),
        ):
            response = await self.get("a", f"runs/{run['id']}/diagnostic-export")
        self.assertEqual(
            response.status_code,
            200,
            response.text[:200] if response.status_code != 200 else "",
        )
        data, m = unpack(response.content)
        self.assertEqual(len(data["artifacts"]), len(detail["artifacts"]))
        self.assertFalse(m["coverage"]["schedulers"]["included"])
        self.assertFalse(m["coverage"]["sync"]["included"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        for index, item in enumerate(detail["artifacts"]):
            await self.set_policy(item["object_ref"], [], key="hide-art-" + str(index))
        await self.set_policy(session, [], key="hide-session")
        data, _ = unpack(
            (await self.get("a", f"runs/{run['id']}/diagnostic-export")).content
        )
        self.assertEqual(data["artifacts"], [])
        self.assertIsNone(data["attempts"][0]["session"])
        self.directory["principals"]["b"]["objects"][run["id"]] = ["run.read"]
        self.save()
        data, m = unpack(
            (await self.get("b", f"runs/{run['id']}/diagnostic-export")).content
        )
        for k in ["tools", "tool_decisions", "artifacts"]:
            self.assertFalse(m["coverage"][k]["included"])
        self.assertEqual(
            (await self.get("c", f"runs/{run['id']}/diagnostic-export")).status_code,
            404,
        )

    async def test_revocation_during_export_fails_closed(self):
        run = await self.submit_as("a")
        await self.finish()
        original = export.build

        async def revoke(*args, **kw):
            result = await original(*args, **kw)
            self.directory["principals"]["a"]["tokens"][0]["enabled"] = False
            self.save()
            return result

        with patch.object(export, "build", revoke):
            response = await self.get("a", f"runs/{run['id']}/diagnostic-export")
        self.assertEqual(response.status_code, 401)
