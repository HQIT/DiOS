import asyncio
import json
import os
import sys
import unittest
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from app.config import settings
from app.models.tables import A2ATask, ManagedTaskAdoption, ManagedTaskExecution
from app.runtime.database import RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.docker_binding import BoundDockerAdapter, inspect_existing
from app.services import (
    a2a_service,
    managed_adoption,
    managed_recovery,
    managed_runtime,
)
from app.services.managed_files import read_result
from app.services.managed_upgrade import Upgrade
from fastapi import FastAPI
from sqlalchemy import func, select
from tests import test_managed_runtime as fixtures


class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagedRuntimeTests.asyncSetUp
    asyncTearDown = fixtures.ManagedRuntimeTests.asyncTearDown
    task = fixtures.ManagedRuntimeTests.task

    async def source(self):
        await self.task()
        self.native = "a" * 64
        self.identity = {
            "provider": "fixture-v1",
            "native_id": self.native,
            "host": "fixture",
            "process_instance": "instance-1",
            "spec_digest": "b" * 64,
            "name": "dios-run-task",
            "run_id": "task",
            "managed_scope": None,
            "kind": None,
            "task_config": "/workspace/agent-task-task.json",
            "workspace": {
                "Type": "bind",
                "Source": str(self.root / "agent"),
                "RW": True,
            },
        }
        self.config = self.root / "agent/agent-task-task.json"
        self.config.write_text(
            json.dumps(
                {
                    "models": {"api_key": "PRIVATE-FIXTURE"},
                    "task": {"task": "{}", "output_dir": "output/events/task"},
                }
            )
        )
        self.adapter.name = "fixture-v1"
        self.adapter.inspect_existing.return_value = {
            "identity": self.identity,
            "observation": asdict(fixtures.observation()),
        }
        self.bound = AsyncMock()
        self.bound.observe.return_value = fixtures.observation()
        self.bound.logs.return_value = self.adapter.logs.return_value
        self.bound.stop.return_value = fixtures.observation("exited", False)
        self.adapter.bind_existing = MagicMock(return_value=self.bound)
        self.stack.enter_context(patch.object(settings, "runtime_database_url", ""))

    async def commit(self, plan):
        return await managed_adoption.adopt(
            "task",
            self.native,
            plan["digest"],
            actor="operator",
            reason="source controller stopped",
            source_quiesced=True,
        )

    async def test_preview_is_read_only_and_hides_config_secrets(self):
        await self.source()
        plan = await managed_adoption.preview("task", self.native)
        self.assertNotIn("PRIVATE-FIXTURE", json.dumps(plan))
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(ManagedTaskExecution)),
                0,
            )
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(ManagedTaskAdoption)),
                0,
            )
        self.adapter.start.assert_not_awaited()
        self.adapter.stop.assert_not_awaited()
        with (
            patch(
                "app.services.managed_runtime.managed_adapter",
                side_effect=AssertionError("provider must not be contacted"),
            ),
            self.assertRaisesRegex(RuntimeErrorCode, "ADOPTION_TASK_NOT_ACTIVE"),
        ):
            await managed_adoption.preview("missing", self.native)

    async def test_requires_drain_confirmation_and_matching_preview(self):
        await self.source()
        plan = await managed_adoption.preview("task", self.native)
        with self.assertRaisesRegex(RuntimeErrorCode, "ADOPTION_DRAIN_REQUIRED"):
            await self.commit(plan)
        await Upgrade().drain()
        with self.assertRaisesRegex(RuntimeErrorCode, "ADOPTION_CONFIRMATION_REQUIRED"):
            await managed_adoption.adopt(
                "task",
                self.native,
                plan["digest"],
                actor="a",
                reason="b",
                source_quiesced=False,
            )
        self.identity["spec_digest"] = "c" * 64
        with self.assertRaisesRegex(RuntimeErrorCode, "ADOPTION_PREVIEW_CHANGED"):
            await self.commit(plan)

    async def test_adopt_original_instance_and_resume_existing_recovery(self):
        await self.source()
        plan = await managed_adoption.preview("task", self.native)
        await Upgrade().drain()
        first = await self.commit(plan)
        self.assertTrue(first["adopted"])
        self.assertEqual(await managed_recovery.reconcile("task"), "pending")
        report = self.root / "agent/output/events/task/task_result.md"
        report.parent.mkdir(parents=True)
        report.write_text("# 原执行报告\n\n来源：fixture", encoding="utf-8")
        self.bound.observe.return_value = fixtures.observation("exited", False)
        self.assertEqual(await managed_recovery.reconcile("task"), "finished")
        self.assertTrue((await self.commit(plan))["repeated"])
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            self.assertEqual(task.status, "completed")
            self.assertIn("原执行报告", task.artifacts[0]["parts"][0]["text"])
            adoption = await db.get(ManagedTaskAdoption, "task")
            self.assertEqual(adoption.source_quiescence, "operator_attested")
        self.adapter.start.assert_not_awaited()
        self.bound.start.assert_not_awaited()
        self.bound.remove.assert_awaited_once()

    async def test_cancel_uses_original_bound_object_and_preserves_revocation(self):
        await self.source()
        plan = await managed_adoption.preview("task", self.native)
        await Upgrade().drain()
        await self.commit(plan)
        async with self.sessions() as db:
            task = await a2a_service.cancel_task(db, "task")
            self.assertEqual(task.status, "canceled")
        self.bound.stop.assert_awaited_once()
        self.adapter.stop.assert_not_awaited()

    async def test_mapping_rejects_wrong_labels_mount_and_input(self):
        await self.source()
        for key, value, code in (
            ("run_id", "different", "ADOPTION_TASK_RUNTIME_MISMATCH"),
            (
                "workspace",
                {"Type": "bind", "Source": "/other", "RW": True},
                "ADOPTION_WORKSPACE_MISMATCH",
            ),
        ):
            original = self.identity[key]
            self.identity[key] = value
            with self.assertRaisesRegex(RuntimeErrorCode, code):
                await managed_adoption.preview("task", self.native)
            self.identity[key] = original
        self.config.write_text(json.dumps({"task": {"task": "another input"}}))
        with self.assertRaisesRegex(RuntimeErrorCode, "ADOPTION_TASK_CONFIG_MISMATCH"):
            await managed_adoption.preview("task", self.native)

    async def test_changed_task_and_corrupt_binding_rejected(self):
        await self.source()
        plan = await managed_adoption.preview("task", self.native)
        await Upgrade().drain()
        async with self.sessions() as db:
            (await db.get(A2ATask, "task")).trace_id = "changed"
            await db.commit()
        with self.assertRaisesRegex(RuntimeErrorCode, "ADOPTION_PREVIEW_CHANGED"):
            await self.commit(plan)
        plan = await managed_adoption.preview("task", self.native)
        await self.commit(plan)
        async with self.sessions() as db:
            row = await db.get(ManagedTaskAdoption, "task")
            row.digest = "0" * 64
            await db.commit()
        self.assertEqual(await managed_recovery.reconcile("task"), "error")
        self.bound.observe.assert_not_awaited()
        async with self.sessions() as db:
            (await db.get(ManagedTaskAdoption, "task")).digest = plan["digest"]
            (await db.get(ManagedTaskExecution, "task")).workspace_path = str(
                self.root / "other"
            )
            await db.commit()
        self.assertEqual(await managed_recovery.reconcile("task"), "error")
        self.bound.observe.assert_not_awaited()

    async def test_missing_process_identity_is_filled_without_overwriting_cancel_intent(
        self,
    ):
        await self.source()
        async with self.sessions() as db:
            row = await managed_recovery.register(
                db,
                "task",
                managed_runtime.handle("task", "task"),
                str(self.root / "agent"),
                "task",
            )
            row.cancel_requested = True
            await db.commit()
        plan = await managed_adoption.preview("task", self.native)
        await Upgrade().drain()
        await self.commit(plan)
        self.assertEqual(await managed_recovery.reconcile("task"), "finished")
        self.bound.stop.assert_awaited_once()

    async def test_adoption_revokes_old_grants_and_duplicate_confirmation_is_idempotent(
        self,
    ):
        from datetime import UTC, datetime, timedelta

        from app.models.tables import E2AGToolGrant, EventLog
        from app.services.e2ag import verify_audit_chain

        await self.source()
        async with self.sessions() as db:
            (await db.get(A2ATask, "task")).context_id = "source-event"
            db.add(
                EventLog(
                    id="source-event",
                    source="fixture",
                    event_type="fixture.review",
                    cloud_event={},
                    trace_id="original-trace",
                )
            )
            db.add(
                E2AGToolGrant(
                    id="original-grant",
                    task_id="task",
                    agent_id="agent",
                    token_hash="fixture",
                    trace_id="trace",
                    mcp_server_id="mcp",
                    allowed_tools=["read"],
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await db.commit()
        plan = await managed_adoption.preview("task", self.native)
        await Upgrade().drain()
        results = await asyncio.gather(self.commit(plan), self.commit(plan))
        self.assertEqual(sorted(x["repeated"] for x in results), [False, True])
        async with self.sessions() as db:
            self.assertEqual(
                (await db.get(E2AGToolGrant, "original-grant")).status, "revoked"
            )
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(E2AGToolGrant)), 1
            )
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(ManagedTaskAdoption)),
                1,
            )
            chain = (await db.get(EventLog, "source-event")).audit_chain
            self.assertTrue(verify_audit_chain(chain))
            self.assertEqual(len(chain), 1)
            self.assertEqual(chain[0]["stage"], "runtime_adoption")

    async def test_api_requires_operator_and_declared_quiescence(self):
        await self.source()
        from app.api.os.runtime_adoption import router

        self.stack.enter_context(
            patch.object(settings, "access_token", "operator-fixture")
        )
        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            body = {"task_id": "task", "native_id": self.native}
            self.assertEqual(
                (
                    await client.post("/runtime/adoptions/preview", json=body)
                ).status_code,
                401,
            )
            response = await client.post(
                "/runtime/adoptions/preview",
                json=body,
                headers={"Authorization": "Bearer operator-fixture"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(
                (
                    await client.post(
                        "/runtime/adoptions",
                        json=body,
                        headers={"Authorization": "Bearer operator-fixture"},
                    )
                ).status_code,
                422,
            )

    async def test_secure_config_and_report_reject_links_and_oversize(self):
        await self.source()
        original = self.root / "secret"
        self.config.rename(original)
        self.config.symlink_to(original)
        with self.assertRaises(OSError):
            await managed_adoption.preview("task", self.native)
        root = self.root / "agent/output/events/task"
        root.mkdir(parents=True)
        (root / "task_result.md").symlink_to(original)
        with self.assertRaises(OSError):
            read_result(self.root / "agent", "task")
        (root / "task_result.md").unlink()
        (root / "nested").mkdir()
        report = root / "nested/task_result.md"
        report.write_text("nested report")
        self.assertEqual(read_result(self.root / "agent", "task"), "nested report")
        with report.open("wb") as output:
            output.truncate(8 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(RuntimeErrorCode, "WORKSPACE_FILE_INVALID"):
            read_result(self.root / "agent", "task")


class DockerBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_binding_pins_id_host_spec_and_process_and_forbids_launch(
        self,
    ):
        container = MagicMock()
        container.id = "a" * 64
        container.attrs = {
            "Id": container.id,
            "Name": "/dios-run-task",
            "Image": "sha256:image",
            "Config": {
                "Env": [
                    "TASK_CONFIG=/workspace/agent-task-task.json",
                    "PRIVATE=secret",
                ],
                "Labels": {"dios.run_id": "task"},
            },
            "HostConfig": {},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/work",
                    "Destination": "/workspace",
                    "RW": True,
                }
            ],
            "State": {
                "StartedAt": "one",
                "Status": "running",
                "Running": True,
                "Pid": 123,
            },
        }
        client = MagicMock()
        client.api.base_url = "http+docker://localhost"
        client.info.return_value = {"ID": "host"}
        client.containers.get.return_value = container
        base = DockerRuntimeAdapter("scope", client=client)
        inspected = await inspect_existing(base, container.id)
        self.assertNotIn("secret", json.dumps(inspected))
        bound = BoundDockerAdapter(base, "handle", inspected["identity"])
        self.assertEqual(
            (await bound.observe("handle")).process_instance, container.id + ":one"
        )
        with self.assertRaisesRegex(
            RuntimeErrorCode, "ADOPTED_RUNTIME_START_FORBIDDEN"
        ):
            await bound.start("handle", None)
        container.attrs["State"]["StartedAt"] = "two"
        with self.assertRaisesRegex(RuntimeErrorCode, "PROCESS_CHANGED"):
            await bound.stop("handle")
        container.stop.assert_not_called()
        container.attrs["State"]["StartedAt"] = "one"
        container.attrs["Config"]["Env"].append("CHANGED=true")
        with self.assertRaisesRegex(RuntimeErrorCode, "RUNTIME_BINDING_CHANGED"):
            await bound.remove("handle")
        container.remove.assert_not_called()
        client.info.return_value = {"ID": "other"}
        with self.assertRaisesRegex(RuntimeErrorCode, "RUNTIME_HOST_CHANGED"):
            await bound.observe("handle")
        from app.runtime import providers
        from docker.errors import DockerException

        with (
            patch.object(
                providers,
                "DockerRuntimeAdapter",
                side_effect=DockerException("private endpoint"),
            ),
            self.assertRaisesRegex(RuntimeErrorCode, "RUNTIME_PROVIDER_UNAVAILABLE"),
        ):
            providers.managed_adapter.__wrapped__()


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1",
    "requires explicit native Docker qualification",
)
class NativeAdoptionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagedRuntimeTests.asyncSetUp
    asyncTearDown = fixtures.ManagedRuntimeTests.asyncTearDown
    task = fixtures.ManagedRuntimeTests.task

    async def qualify(self, cancel=False):
        import docker
        from docker.errors import NotFound

        await self.task()
        task_id = uuid4().hex
        run_id = task_id[:12]
        async with self.sessions() as db:
            (await db.get(A2ATask, "task")).id = task_id
            await db.commit()
        root = self.root / "agent"
        (root / f"agent-task-{run_id}.json").write_text(
            json.dumps(
                {"task": {"task": "{}", "output_dir": f"output/events/{run_id}"}}
            )
        )
        client = docker.from_env(timeout=5)
        adapter = DockerRuntimeAdapter(
            settings.runtime_scope + ":managed", client=client
        )
        self.stack.enter_context(
            patch("app.services.managed_runtime.managed_adapter", return_value=adapter)
        )
        self.stack.enter_context(patch.object(settings, "runtime_database_url", ""))
        code = (
            "import pathlib,time,os; p=pathlib.Path('/workspace/release'); print('native original process',flush=True)\nwhile not p.exists(): time.sleep(.05)\nr=pathlib.Path('/workspace/output/events/"
            + run_id
            + "/task_result.md'); r.parent.mkdir(parents=True,exist_ok=True); r.write_text('# Native original result\\n\\nEvidence: synthetic-source',encoding='utf-8')"
        )
        container = await asyncio.to_thread(
            client.containers.run,
            "dios-backend:service-recovery-20261124",
            command=["python", "-u", "-c", code],
            name="dios-run-" + run_id,
            labels={"dios.run_id": run_id},
            environment={"TASK_CONFIG": f"/workspace/agent-task-{run_id}.json"},
            volumes={str(root): {"bind": "/workspace", "mode": "rw"}},
            network_mode="none",
            user=f"{os.getuid()}:{os.getgid()}",
            detach=True,
            auto_remove=False,
        )
        try:
            await asyncio.to_thread(container.reload)
            before = container.id + ":" + container.attrs["State"]["StartedAt"]
            env = dict(
                os.environ,
                DIOS_DATABASE_URL=str(self.engine.url),
                DIOS_RUNTIME_SCOPE=settings.runtime_scope,
                DIOS_WORKSPACE_ROOT=str(self.root),
                DIOS_MANAGED_RUNTIME_LOCK_DIR=str(self.root / "locks"),
                DIOS_RUNTIME_DATABASE_URL="",
            )

            async def cli(*args):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.services.managed_adoption",
                    *args,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(process.communicate(), 20)
                self.assertEqual(process.returncode, 0, (out + err).decode())
                return json.loads(out)

            plan = await cli("preview", task_id, container.id)
            self.assertEqual(plan["record"]["identity"]["process_instance"], before)
            await Upgrade().drain()
            result = await cli(
                "adopt",
                task_id,
                container.id,
                "--expected-digest",
                plan["digest"],
                "--actor",
                "qualification-operator",
                "--reason",
                "synthetic source has no prior controller",
                "--source-quiesced",
            )
            self.assertTrue(result["adopted"])
            self.assertEqual(await managed_recovery.reconcile(task_id), "pending")
            await asyncio.to_thread(container.reload)
            self.assertEqual(
                container.id + ":" + container.attrs["State"]["StartedAt"], before
            )
            if cancel:
                async with self.sessions() as db:
                    self.assertEqual(
                        (await a2a_service.cancel_task(db, task_id)).status, "canceled"
                    )
            else:
                (root / "release").write_text("finish")
                await asyncio.to_thread(container.wait, timeout=15)
                self.assertEqual(await managed_recovery.reconcile(task_id), "finished")
                async with self.sessions() as db:
                    task = await db.get(A2ATask, task_id)
                    self.assertEqual(task.status, "completed")
                    self.assertIn(
                        "Native original result", task.artifacts[0]["parts"][0]["text"]
                    )
            with self.assertRaises(NotFound):
                await asyncio.to_thread(client.containers.get, container.id)
            self.assertTrue(
                (
                    await cli(
                        "adopt",
                        task_id,
                        container.id,
                        "--expected-digest",
                        plan["digest"],
                        "--actor",
                        "qualification-operator",
                        "--reason",
                        "repeat original identity",
                        "--source-quiesced",
                    )
                )["repeated"]
            )
        finally:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except NotFound:
                pass
            client.close()

    async def test_original_container_report_recovered_through_independent_cli(self):
        await self.qualify()

    async def test_cancel_controls_original_adopted_container(self):
        await self.qualify(cancel=True)
