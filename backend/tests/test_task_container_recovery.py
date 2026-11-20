"""通过 DiOS 启动流程和任务查询接口验证一次性容器恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from docker.errors import NotFound
from requests.exceptions import ConnectionError
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import database
from app.config import settings
from app.models.tables import A2ATask, Agent, EventLog, ModelInferenceGrant, TaskInferenceGrant
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

    async def test_recovered_task_exposes_its_report_through_a2a_and_download(self) -> None:
        self.set_container("exited")
        result_dir = self.workspace / "output" / "events" / RUN_ID / "run-fixture"
        result_dir.mkdir(parents=True)
        (result_dir / "task_result.md").write_text("评审已完成", encoding="utf-8")
        report_path = result_dir / "reviewer-report.md"
        report_path.write_text("# 评审报告\n原始结论", encoding="utf-8")
        (result_dir / "task.log").write_text("执行日志", encoding="utf-8")

        async with app.router.lifespan_context(app):
            task = await self.get_terminal_task()
            self.assertEqual(task["status"], "completed")
            self.assertEqual([item["name"] for item in task["artifacts"]], ["task_result.md", "reviewer-report.md"])
            report = task["artifacts"][1]
            self.assertEqual(report["parts"][0]["text"], "# 评审报告\n原始结论")
            report_path.write_text("后来修改的文件", encoding="utf-8")
            download = await self.http.get(f"/api/os/a2a/tasks/{TASK_ID}/artifacts/{report['artifactId']}")
            overview = await self.http.get("/api/os/events/activity-gantt")

        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertIn("reviewer-report.md", download.headers["content-disposition"])
        self.assertEqual(download.text, "# 评审报告\n原始结论")
        overview.raise_for_status()
        self.assertEqual(overview.json()["bars"][0]["artifacts_count"], 2)
        self.assertNotIn("原始结论", overview.text)

    async def test_recovery_only_collects_regular_reports_from_the_selected_run(self) -> None:
        self.set_container("exited")
        output = self.workspace / "output" / "events" / RUN_ID
        archive = output / "run-old"
        selected = output / "run-new"
        archive.mkdir(parents=True)
        selected.mkdir()
        old_primary = archive / "task_result.md"
        old_primary.write_text("旧结果", encoding="utf-8")
        os.utime(old_primary, (1, 1))
        (archive / "stale.md").write_text("旧报告", encoding="utf-8")
        (selected / "task_result.md").write_text("最新结果", encoding="utf-8")
        (selected / "report.md").write_text("有效报告", encoding="utf-8")
        outside = self.workspace / "private.md"
        outside.write_text("超出产物目录的内容", encoding="utf-8")
        os.link(outside, selected / "hardlink.md")
        if os.name != "nt":
            (selected / "symlink.md").symlink_to(outside)
            os.mkfifo(selected / "pipe.md")
        (selected / "oversized.md").write_bytes(b"a" * (256 * 1024 + 1))
        (selected / "invalid.md").write_bytes(b"\xff")

        async with app.router.lifespan_context(app):
            task = await self.get_terminal_task()

        self.assertEqual([item["name"] for item in task["artifacts"]], ["task_result.md", "report.md"])
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "最新结果")

    async def test_artifact_download_requires_the_control_plane_token_when_enabled(self) -> None:
        with patch.object(settings, "access_token", "fixture-admin"):
            response = await self.http.get(f"/api/os/a2a/tasks/{TASK_ID}/artifacts/{TASK_ID}-result")
        self.assertEqual(response.status_code, 401)

    async def test_activity_views_can_show_a_running_task_before_artifacts_exist(self) -> None:
        async with database.async_session() as db:
            db.add(EventLog(id="fixture-event", source="manual/test", event_type="fixture", cloud_event={}))
            task = await db.get(A2ATask, TASK_ID)
            task.context_id = "fixture-event"
            await db.commit()

        gantt = await self.http.get("/api/os/events/activity-gantt")
        self.assertEqual(gantt.status_code, 200, gantt.text)
        overview = await self.http.get("/api/os/events/fixture-event/activity-overview")
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(gantt.json()["bars"][0]["status"], "working")
        self.assertEqual(overview.json()["items"][0]["artifacts_count"], 0)

    async def test_startup_reaps_a_created_but_unstarted_managed_task(self) -> None:
        container = self.set_container("created")
        bearer = "x" * 43
        digest = hashlib.sha256(bearer.encode()).hexdigest()
        async with database.async_session() as db:
            task = await db.get(A2ATask, TASK_ID)
            task.status = "submitted"
            db.add(ModelInferenceGrant(token_hash=digest, agent_id="reviewer", model_id="fixture",
                                       model_name="fixture", expires_at=int(time.time()) + 300, remaining_calls=1))
            db.add(TaskInferenceGrant(task_id=TASK_ID, token_hash=digest))
            await db.commit()

        with patch.object(settings, "diagent_task_model_proxy", True), patch.object(settings, "access_token", "fixture-admin"):
            self.http.headers["Authorization"] = "Bearer fixture-admin"
            async with app.router.lifespan_context(app):
                task = await self.get_terminal_task()
            self.assertEqual(task["status"], "failed")
            self.assertIn("not started", task["error"])
            replay = await self.http.post("/api/internal/model-proxy/v1/chat/completions", headers={
                "Authorization": "Bearer " + bearer,
            }, json={"model": "fixture", "messages": []})
            self.assertEqual(replay.status_code, 401, replay.text)

        container.remove.assert_called_once_with(force=True)

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

    async def test_startup_ignores_result_links_when_collecting_task_artifacts(self) -> None:
        self.set_container("exited")
        result = self.workspace / "output" / "events" / RUN_ID / "task_result.md"
        result.parent.mkdir(parents=True)
        unrelated = self.workspace / "unrelated.txt"
        unrelated.write_text("fixture outside the result directory", encoding="utf-8")
        try:
            result.symlink_to(unrelated)
        except OSError:
            self.skipTest("Symbolic links are unavailable in this test environment")

        async with app.router.lifespan_context(app):
            task = await self.get_terminal_task()

        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["artifacts"], [])

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
