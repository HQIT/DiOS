"""通过 DiOS 启动流程和任务查询接口验证一次性容器恢复。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from docker.errors import NotFound
from requests.exceptions import ConnectionError
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import database
from app.models.tables import A2ATask, Agent
from app.services import a2a_service, cron_scheduler, docker_runner, event_retry_worker, imap_poller
from main import app


TASK_ID = "55ef021004a045378cf9e66cb75a1c79"
RUN_ID = "55ef021004a0"


class TaskContainerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        workspace_root = Path(__file__).resolve().parents[2] / "workspace"
        workspace_root.mkdir(exist_ok=True)
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=workspace_root))
        self.workspace = Path(directory)
        engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(self.workspace / "dios.db")))
        self.addAsyncCleanup(engine.dispose)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.enterContext(patch.object(database, "engine", engine))
        self.enterContext(patch.object(database, "async_session", sessions))
        self.enterContext(patch.object(cron_scheduler, "async_session", sessions))
        self.enterContext(patch.object(imap_poller, "async_session", sessions))
        self.enterContext(patch.object(event_retry_worker, "async_session", sessions))
        self.enterContext(patch.object(a2a_service, "_TASK_POLL_INTERVAL", 0.01))
        await database.init_db()
        async with sessions() as db:
            db.add(Agent(id="reviewer", name="Reviewer", mode="task", workspace_path=directory))
            db.add(A2ATask(id=TASK_ID, agent_id="reviewer", status="working", message={}))
            await db.commit()
        self.docker = MagicMock()
        self.enterContext(patch.object(docker_runner, "_client", self.docker))
        self.http = await self.enterAsyncContext(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dios")
        )

    def set_container(self, status: str, exit_code: int = 0) -> MagicMock:
        container = self.docker.containers.get.return_value
        container.id = "container-id"
        container.labels = {"dios.run_id": RUN_ID}
        container.status = status
        container.attrs = {"State": {"ExitCode": exit_code}}
        container.logs.return_value = b""
        return container

    async def get_task(self) -> dict:
        response = await self.http.get(f"/api/os/a2a/tasks/{TASK_ID}")
        response.raise_for_status()
        return response.json()

    async def get_terminal_task(self) -> dict:
        async with asyncio.timeout(2):
            while (task := await self.get_task())["status"] == "working":
                await asyncio.sleep(0.01)
            return task

    async def test_startup_collects_result_from_exited_container(self) -> None:
        self.set_container("exited")
        result_dir = self.workspace / "output" / "events" / RUN_ID
        result_dir.mkdir(parents=True)
        (result_dir / "task_result.md").write_text("评审已完成", encoding="utf-8")

        async with app.router.lifespan_context(app):
            task = await self.get_terminal_task()

        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "评审已完成")

    async def test_startup_resumes_monitoring_running_container(self) -> None:
        container = self.set_container("running", exit_code=7)
        container.logs.return_value = b"model failure"
        inspected = asyncio.Event()
        loop = asyncio.get_running_loop()
        container.reload.side_effect = lambda: loop.call_soon_threadsafe(inspected.set)

        async with app.router.lifespan_context(app):
            await asyncio.wait_for(inspected.wait(), timeout=2)
            self.assertEqual((await self.get_task())["status"], "working")
            container.status = "exited"
            task = await self.get_terminal_task()

        self.assertEqual(task["status"], "failed")
        self.assertIn("container exit code 7", task["error"])
        self.assertIn("model failure", task["error"])

    async def test_startup_marks_task_failed_when_docker_confirms_container_missing(self) -> None:
        self.docker.containers.get.side_effect = NotFound("missing")

        async with app.router.lifespan_context(app):
            task = await self.get_terminal_task()

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"], "Task container is missing")

    async def test_startup_retries_when_docker_is_temporarily_unavailable(self) -> None:
        container = self.set_container("exited")
        calls = iter([ConnectionError("unavailable")])

        def inspect_container(_reference: str) -> MagicMock:
            error = next(calls, None)
            if error is not None:
                raise error
            return container

        self.docker.containers.get.side_effect = inspect_container

        async with app.router.lifespan_context(app):
            task = await self.get_terminal_task()

        self.assertEqual(task["status"], "completed")
