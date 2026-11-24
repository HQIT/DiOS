"""Explicit qualification with independent schedulers and a native synthetic task."""

import asyncio
import os
import sys
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from app.config import settings
from app.models.tables import A2ATask, Agent, CronOccurrence, EventLog, Subscription
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.services import durable_cron as cron
from app.services import managed_recovery, managed_runtime
from app.services.e2ag import verify_audit_chain
from sqlalchemy import select
from tests import test_managed_runtime as fixtures

CHILD = r"""
import asyncio,sys,os
from datetime import datetime
from app.runtime.adapter import LaunchSpec,Mount
from app.services import managed_runtime
from app.services.durable_cron import CronScheduler
code = "import json,os,pathlib,time; c=json.loads(pathlib.Path(os.environ['TASK_CONFIG']).read_text()); p=pathlib.Path('/workspace/release'); print('native scheduled task',flush=True)\nwhile not p.exists(): time.sleep(.05)\nr=pathlib.Path('/workspace')/c['task']['output_dir']/'task_result.md'; r.parent.mkdir(parents=True,exist_ok=True); r.write_text('# Scheduled native report\\n\\nEvidence: synthetic-source',encoding='utf-8')"
def launch(kind, workspace, environment):
    return LaunchSpec('dios-backend:service-recovery-20261124', ('python','-u','-c',code),
        kind=kind, environment=environment, mounts=(Mount(str(workspace),'/workspace',False),),
        working_dir='/app', user=f'{os.getuid()}:{os.getgid()}', network='none')
managed_runtime.launch_spec=launch
asyncio.run(CronScheduler()._tick(datetime.fromisoformat(sys.argv[1])))
"""


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1",
    "requires explicit native Docker qualification",
)
class NativeCronTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagedRuntimeTests.asyncSetUp
    asyncTearDown = fixtures.ManagedRuntimeTests.asyncTearDown

    async def test_independent_processes_reuse_original_execution_after_restart(self):
        import docker

        self.stack.enter_context(
            patch.object(settings, "runtime_scope", "cron-test-" + uuid4().hex)
        )
        self.stack.enter_context(patch.object(settings, "e2ag_mode", "enforce"))
        client = docker.from_env(timeout=10)
        adapter = DockerRuntimeAdapter(
            settings.runtime_scope + ":managed", client=client
        )
        self.stack.enter_context(
            patch.object(managed_runtime, "managed_adapter", return_value=adapter)
        )
        start = datetime(2026, 11, 24, tzinfo=UTC)
        async with self.sessions() as db:
            agent = await db.get(Agent, "agent")
            agent.capabilities = {
                "governance": {
                    "allowed_event_sources": ["cron/*"],
                    "allowed_event_types": ["cron.tick"],
                }
            }
            sub = Subscription(
                id="native",
                agent_id="agent",
                source_pattern="cron/*",
                event_types=["cron.tick"],
                cron_expression="* * * * *",
                enabled=True,
            )
            db.add(sub)
            await db.flush()
            await cron.synchronize(db, sub, start)
            await db.commit()
        env = dict(
            os.environ,
            DIOS_DATABASE_URL=str(self.engine.url),
            DIOS_RUNTIME_SCOPE=settings.runtime_scope,
            DIOS_WORKSPACE_ROOT=str(self.root),
            DIOS_MANAGED_RUNTIME_LOCK_DIR=str(self.root / "locks"),
            DIOS_E2AG_MODE="enforce",
        )
        children, container = [], None

        async def tick():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                CHILD,
                "2026-11-24T00:01:00+00:00",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            children.append(process)
            out, err = await asyncio.wait_for(process.communicate(), 30)
            self.assertEqual(0, process.returncode, (out + err).decode())

        try:
            await asyncio.gather(tick(), tick())
            async with self.sessions() as db:
                tasks = list((await db.scalars(select(A2ATask))).all())
                self.assertEqual(1, len(tasks))
                task_id = tasks[0].id
                self.assertEqual("working", tasks[0].status, tasks[0].error)
            handle = managed_runtime.handle("task", task_id)
            first = await adapter.observe(handle)
            self.assertTrue(first.alive)
            # Only the qualified task's immutable native object is touched.
            native_id = first.process_instance.split(":", 1)[0]
            container = await asyncio.to_thread(client.containers.get, native_id)
            await tick()
            second = await adapter.observe(handle)
            self.assertEqual(first.process_instance, second.process_instance)
            (self.root / "agent/release").write_text("finish")
            await asyncio.to_thread(container.wait, timeout=15)
            self.assertEqual("finished", await managed_recovery.reconcile(task_id))
            await tick()
            async with self.sessions() as db:
                tasks = list((await db.scalars(select(A2ATask))).all())
                self.assertEqual(1, len(tasks))
                self.assertEqual("completed", tasks[0].status)
                self.assertIn(
                    "Scheduled native report", tasks[0].artifacts[0]["parts"][0]["text"]
                )
                items = list((await db.scalars(select(CronOccurrence))).all())
                self.assertEqual(["succeeded"], [i.state for i in items])
                logs = list((await db.scalars(select(EventLog))).all())
                self.assertEqual(1, len(logs))
                self.assertTrue(verify_audit_chain(logs[0].audit_chain))
        finally:
            for process in children:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
            if container is not None:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except docker.errors.NotFound:
                    pass
            client.close()
