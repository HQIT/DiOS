"""验证模型代理与模型管理接口的凭据边界。"""

import asyncio
import json
import threading
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import anyio
import httpx
from docker.errors import NotFound
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.database import Base, get_db
from app.config import settings
from app.models.tables import Agent, AgentRuntime, E2AGToolGrant, LLMModel, McpServer, ModelInferenceGrant, Skill
from app.services.agent_runtime import ensure_running
from app.services.model_proxy import model_grant
from main import app


class ModelProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.resources = AsyncExitStack()
        self.addAsyncCleanup(self.resources.aclose)
        workspace_root = Path(__file__).resolve().parents[2] / "workspace"
        workspace_root.mkdir(exist_ok=True)
        self.fixture_root = Path(self.resources.enter_context(TemporaryDirectory(dir=workspace_root)))
        engine = create_async_engine(f"sqlite+aiosqlite:///{(self.fixture_root / 'fixture.db').as_posix()}")
        self.resources.push_async_callback(engine.dispose)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def get_test_db():
            async with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = get_test_db
        self.resources.callback(app.dependency_overrides.pop, get_db)
        self.client = await self.resources.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dios")
        )

    async def test_model_key_can_be_written_but_is_never_returned(self):
        credential = "fixture-provider-credential"
        created = await self.client.post("/api/os/models", json={
            "name": "reviewer", "model": "fixture-model", "base_url": "https://model.test/v1",
            "api_key": credential,
        })
        self.assertEqual(created.status_code, 201)
        self.assertNotIn(credential, created.text)
        model_path = f"/api/os/models/{created.json()['id']}"
        updated = await self.client.put(model_path, json={"display_name": "Reviewer model"})
        self.assertEqual(updated.status_code, 200)
        self.assertNotIn(credential, updated.text)
        for path in (model_path, "/api/os/models"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(credential, response.text)
        async with self.sessions() as db:
            model = await db.get(LLMModel, created.json()["id"])
            self.assertEqual(model.api_key, credential)

    async def test_managed_chat_dispatches_each_agents_explicit_tool_selection(self):
        async with self.sessions() as db:
            model = LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1")
            db.add(model)
            await db.flush()
            reviewer = Agent(name="Reviewer", model=model.id, skills=["review"],
                             capabilities={"diagent_tools": ["skill"]})
            writer = Agent(name="Writer", model=model.id, skills=["draft"],
                           capabilities={"diagent_tools": []})
            db.add_all([reviewer, writer])
            await db.commit()

        forwarded = []

        def upstream(request):
            forwarded.append(json.loads(request.content).get("tool_selection"))
            return httpx.Response(200, json={"choices": [{"message": {"content": "Accepted"}}]})

        with (
            patch.object(settings, "diagent_service_model_proxy", True),
            patch.object(settings, "access_token", "fixture-admin"),
            patch("app.api.apps.chat.ensure_running", new=AsyncMock(return_value="http://diagent")),
            patch("app.api.apps.chat.internal_client",
                  side_effect=lambda **opts: httpx.AsyncClient(transport=httpx.MockTransport(upstream), **opts)),
        ):
            for agent in (reviewer, writer):
                response = await self.client.post("/api/apps/chat/completions", headers={"Authorization": "Bearer fixture-admin"}, json={
                    "agent_id": agent.id, "messages": [{"role": "user", "content": "Continue"}], "stream": False,
                    "tool_selection": {"tool_ids": ["shell"]},
                })
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(forwarded, [{"tool_ids": ["skill"]}, {"tool_ids": []}])

    async def test_a2a_task_dispatches_only_selected_tools_and_mcp_credentials(self):
        async with self.sessions() as db:
            model = LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1")
            ledger = McpServer(name="查询工具", transport="streamable_http", url="https://mcp.test/gateway",
                               headers={"Authorization": "Bearer fixture-upstream"})
            skill = Skill(name="fixture-skill", content="Use the fixture instructions.")
            db.add_all([model, ledger, skill])
            await db.commit()

        forwarded = []

        def start_container(run_id, workspace, env, *, task_files, skill_sources):
            self.assertFalse((workspace / f"agent-task-{run_id}.json").exists())
            self.assertEqual(list(workspace.glob("config/mcp_servers_*.json")), [])
            forwarded.append((task_files, skill_sources))
            return "fixture-container"

        with (
            patch.object(settings, "e2ag_mode", "enforce"),
            patch.object(settings, "workspace_root", self.fixture_root),
            patch("app.services.docker_runner.start_container", side_effect=start_container),
            patch("app.services.docker_runner.get_container_status", return_value="running"),
        ):
            for index, tools in enumerate(([], ["mcp", "skill"])):
                response = await self.client.post("/api/os/agents", json={
                    "name": f"Task {index}", "mode": "task", "workspace_path": str(self.fixture_root / f"agent-{index}"),
                    "model": model.name, "mcp_server_ids": [ledger.id], "skills": [skill.name],
                    "capabilities": {"diagent_tools": tools, "governance": {"allowed_tools": ["lookup"]}},
                })
                self.assertEqual(response.status_code, 201, response.text)
                agent_id = response.json()["id"]
                sent = await self.client.post(f"/api/os/a2a/agents/{agent_id}", json={
                    "jsonrpc": "2.0", "id": 1, "method": "message/send",
                    "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Continue"}]}},
                })
                self.assertEqual(sent.json()["result"]["status"]["state"], "working", sent.text)

        files, skills = forwarded[0]
        self.assertEqual(files["task.json"]["task"]["tool_selection"], {"tool_ids": []})
        self.assertNotIn("mcp.json", files)
        self.assertEqual(skills, {})
        files, skills = forwarded[1]
        config = files["task.json"]["task"]
        self.assertEqual(config["tool_selection"], {"tool_ids": ["mcp", "skill"]})
        self.assertEqual(config["mcp_config_path"], "/run/diagent/mcp.json")
        self.assertEqual(config["skills_dir"], "/run/diagent/skills")
        self.assertIn("Use the fixture instructions.", (skills[skill.name] / "SKILL.md").read_text(encoding="utf-8"))
        connection = files["mcp.json"][ledger.id]
        self.assertIn("/api/internal/e2ag/mcp/", connection["url"])
        self.assertNotEqual(connection["headers"]["Authorization"], "Bearer fixture-upstream")

    async def test_a2a_task_dispatches_named_subagents_with_independent_tools_and_one_model(self):
        async with self.sessions() as db:
            db.add(LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1",
                            api_key="fixture-provider-key"))
            await db.commit()

        subagents = [
            {"name": "reviewer", "description": "Review", "prompt": "Review the report.",
             "tool_selection": {"tool_ids": ["read"]}},
            {"name": "summarizer", "description": "Summarize", "prompt": "Summarize the findings.",
             "tool_selection": {"tool_ids": []}},
        ]
        with (
            patch.object(settings, "diagent_task_model_proxy", True),
            patch.object(settings, "diagent_task_model_refresh", True),
            patch.object(settings, "access_token", "fixture-admin"),
            patch.object(settings, "workspace_root", self.fixture_root),
            patch("app.services.docker_runner.start_container", return_value="fixture-container") as launch,
        ):
            admin = {"Authorization": "Bearer fixture-admin"}
            created = await self.client.post("/api/os/agents", headers=admin, json={
                "name": "Task", "mode": "task", "model": "fixture",
                "capabilities": {"diagent_tools": [], "subagents": subagents},
            })
            self.assertEqual(created.status_code, 201, created.text)
            agent_id = created.json()["id"]
            sent = await self.client.post(f"/api/os/a2a/agents/{agent_id}", headers=admin, json={
                "jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Review this."}]}},
            })
            self.assertEqual(sent.json()["result"]["status"]["state"], "working", sent.text)

        files = launch.call_args.kwargs["task_files"]
        self.assertEqual(created.json()["capabilities"]["subagents"], subagents)
        self.assertEqual(files["task.json"]["task"]["subagents"], subagents)
        self.assertEqual(files["task.json"]["task"]["tool_selection"], {"tool_ids": []})
        self.assertEqual(files["task.json"]["task"]["model"], "fixture")
        self.assertEqual(list(files["task.json"]["models"]["models"]), ["fixture"])
        self.assertIn("model-refresh.json", files)
        self.assertNotIn("fixture-provider-key", json.dumps(files))

    async def test_a2a_task_mounts_subagent_skills_separately_from_parent_skills(self):
        async with self.sessions() as db:
            skill = Skill(name="review-skill", content="Review instructions.")
            db.add_all([
                LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1"),
                skill,
            ])
            await db.commit()

        role = {
            "name": "reviewer", "description": "Review", "prompt": "Review the report.",
            "tool_selection": {"tool_ids": ["skill"]}, "skill_names": ["review-skill"],
        }
        with (
            patch.object(settings, "diagent_task_model_proxy", True),
            patch.object(settings, "diagent_task_model_refresh", False),
            patch.object(settings, "access_token", "fixture-admin"),
            patch.object(settings, "workspace_root", self.fixture_root),
            patch("app.services.docker_runner.start_container", return_value="fixture-container") as launch,
        ):
            admin = {"Authorization": "Bearer fixture-admin"}
            created = await self.client.post("/api/os/agents", headers=admin, json={
                "name": "Task", "mode": "task", "model": "fixture",
                "capabilities": {"subagents": [role]},
            })
            self.assertEqual(created.status_code, 201, created.text)
            sent = await self.client.post(f"/api/os/a2a/agents/{created.json()['id']}", headers=admin, json={
                "jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Review."}]}},
            })
            self.assertEqual(sent.json()["result"]["status"]["state"], "working", sent.text)
            deleted = await self.client.delete(f"/api/os/skills/{skill.id}", headers=admin)
            self.assertEqual(deleted.status_code, 409, deleted.text)

        self.assertEqual(created.json()["capabilities"]["subagents"], [role])
        self.assertEqual(launch.call_args.kwargs["skill_sources"], {})
        written = launch.call_args.kwargs["task_files"]["task.json"]["task"]["subagents"]
        self.assertEqual(written, [dict(role, skills_dir="/run/diagent/subagent-skills")])
        sources = launch.call_args.kwargs["subagent_skill_sources"]
        self.assertIn("Review instructions.", (sources["review-skill"] / "SKILL.md").read_text(encoding="utf-8"))

    async def test_a2a_task_issues_independent_mcp_grants_for_two_subagents(self):
        async with self.sessions() as db:
            server = McpServer(name="ledger", transport="streamable_http", url="https://mcp.test/gateway",
                               headers={"Authorization": "Bearer fixture-upstream"})
            db.add_all([LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1"), server])
            await db.commit()
        subagents = [
            {"name": name, "description": "Query " + name, "prompt": "Query the assigned data.",
             "tool_selection": {"tool_ids": ["mcp"]}, "mcp_server_ids": [server.id], "mcp_tools": [name]}
            for name in ("alpha", "beta")
        ]
        with (
            patch.object(settings, "diagent_task_model_proxy", True),
            patch.object(settings, "diagent_task_model_refresh", False),
            patch.object(settings, "e2ag_mode", "enforce"),
            patch.object(settings, "access_token", "fixture-admin"),
            patch.object(settings, "workspace_root", self.fixture_root),
            patch("app.services.docker_runner.start_container", return_value="fixture-container") as launch,
        ):
            admin = {"Authorization": "Bearer fixture-admin"}
            outside_policy = await self.client.post("/api/os/agents", headers=admin, json={
                "name": "Task", "mode": "task", "model": "fixture",
                "capabilities": {"diagent_tools": [], "subagents": subagents,
                                 "governance": {"allowed_tools": ["alpha"]}},
            })
            self.assertEqual(outside_policy.status_code, 422, outside_policy.text)
            self.assertIn("governance", outside_policy.json()["detail"])
            default_shell = await self.client.post("/api/os/agents", headers=admin, json={
                "name": "Task", "mode": "task", "model": "fixture",
                "capabilities": {"subagents": subagents, "governance": {"allowed_tools": ["alpha", "beta"]}},
            })
            self.assertEqual(default_shell.status_code, 422, default_shell.text)
            self.assertIn("without shell", default_shell.json()["detail"])
            created = await self.client.post("/api/os/agents", headers=admin, json={
                "name": "Task", "mode": "task", "model": "fixture",
                "capabilities": {"diagent_tools": [], "subagents": subagents,
                                 "governance": {"allowed_tools": ["alpha", "beta"]}},
            })
            self.assertEqual(created.status_code, 201, created.text)
            sent = await self.client.post(f"/api/os/a2a/agents/{created.json()['id']}", headers=admin, json={
                "jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Consult both."}]}},
            })
            self.assertEqual(sent.json()["result"]["status"]["state"], "working", sent.text)
        files = launch.call_args.kwargs["task_files"]
        self.assertNotIn("mcp.json", files)
        self.assertNotIn("fixture-upstream", json.dumps(files))
        written = files["task.json"]["task"]["subagents"]
        self.assertEqual([role["mcp_config_path"] for role in written],
                         ["/run/diagent/subagent-alpha-mcp.json", "/run/diagent/subagent-beta-mcp.json"])
        self.assertNotIn("mcp_server_ids", written[0])
        async with self.sessions() as db:
            grants = list(await db.scalars(select(E2AGToolGrant)))
        self.assertEqual(sorted((grant.mcp_server_id, grant.allowed_tools) for grant in grants),
                         [(server.id, ["alpha"]), (server.id, ["beta"])])

    async def test_dsh_subagent_file_tools_require_refresh_when_saving_an_agent(self):
        with (
            patch.object(settings, "diagent_task_model_proxy", True),
            patch.object(settings, "diagent_task_model_refresh", False),
        ):
            created = await self.client.post("/api/os/agents", json={
                "name": "Task", "mode": "task",
                "capabilities": {"subagents": [{
                    "name": "reviewer", "description": "Review", "prompt": "Review.",
                    "tool_selection": {"tool_ids": ["read"]},
                }]},
            })
        self.assertEqual(created.status_code, 422, created.text)
        self.assertIn("grant refresh", created.json()["detail"])

    async def test_saving_a_legacy_subagent_keeps_its_mcp_tool_names(self):
        created = await self.client.post("/api/os/agents", json={
            "name": "Legacy task", "mode": "task", "workspace_path": str(self.fixture_root / "legacy"),
            "capabilities": {"subagents": [{
                "name": "reviewer", "description": "Review", "prompt": "Review.", "tools": ["read"],
            }]},
        })
        self.assertEqual(created.status_code, 201, created.text)
        role = created.json()["capabilities"]["subagents"][0]
        self.assertEqual(role["tools"], ["read"])
        self.assertNotIn("tool_selection", role)

    async def test_task_rejects_mixing_legacy_and_dsh_subagent_tools(self):
        with patch.object(settings, "diagent_task_model_proxy", True):
            created = await self.client.post("/api/os/agents", json={
                "name": "Task", "mode": "task",
                "capabilities": {"subagents": [
                    {"name": "reviewer", "description": "Review", "prompt": "Review.",
                     "tool_selection": {"tool_ids": []}},
                    {"name": "legacy", "description": "Legacy", "prompt": "Review.", "tools": ["read"]},
                ]},
            })
        self.assertEqual(created.status_code, 422, created.text)
        self.assertIn("requires tool_selection", created.json()["detail"])

    async def test_a2a_task_model_grant_covers_the_configured_runtime_without_shipping_provider_credentials(self):
        provider_key = "fixture-task-provider-key"
        async with self.sessions() as db:
            model = LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1", api_key=provider_key)
            agent = Agent(name="Task", mode="task", model="fixture", workspace_path=str(self.fixture_root / "agent"),
                          env={"OPENAI_API_KEY": provider_key, "CUSTOM_PROVIDER_KEY": provider_key,
                               "CUSTOM_CONTROL_AUTH": "Bearer fixture-admin", "BUSINESS_SETTING": "keep",
                               "DIAGENT_TASK_TIMEOUT_MS": "7.2e6"})
            db.add_all([model, agent])
            await db.commit()

        launch = {}

        def start_container(run_id, workspace, env, *, task_files, **options):
            launch.update(files=task_files, env=env, workspace=workspace)
            return "fixture-container"

        def upstream(request):
            self.assertEqual(str(request.url), "https://model.test/v1/chat/completions")
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + provider_key)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Accepted"}}]})

        now = 1_790_000_000
        with (
            patch.object(settings, "diagent_task_model_proxy", True),
            patch.object(settings, "diagent_service_model_proxy", False),
            patch.object(settings, "access_token", "fixture-admin"),
            patch.object(settings, "workspace_root", self.fixture_root),
            patch("app.services.model_proxy.time") as clock,
            patch("app.services.docker_runner.start_container", side_effect=start_container),
            patch("app.api.internal.model_proxy.internal_client",
                  side_effect=lambda **opts: httpx.AsyncClient(transport=httpx.MockTransport(upstream), **opts)),
        ):
            clock.time.side_effect = lambda: now
            admin = {"Authorization": "Bearer fixture-admin"}
            path = f"/api/os/a2a/agents/{agent.id}"
            sent = await self.client.post(path, headers=admin, json={
                "jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Continue"}]}},
            })
            task = sent.json()["result"]
            self.assertEqual(task["status"]["state"], "working", sent.text)
            self.assertIsNotNone(launch["files"], "Managed task must install its model configuration privately")
            self.assertNotIn(provider_key, json.dumps([launch["files"], launch["env"]]))
            self.assertEqual(launch["env"], {"BUSINESS_SETTING": "keep", "DIAGENT_TASK_TIMEOUT_MS": "7200000"})
            self.assertEqual(list(launch["workspace"].glob("agent-task-*.json")), [])
            entry = launch["files"]["task.json"]["models"]["models"][model.name]
            self.assertTrue(entry["base_url"].endswith("/api/internal/model-proxy/v1"))
            bearer = {"Authorization": "Bearer " + entry["api_key"]}
            inference = "/api/internal/model-proxy/v1/chat/completions"
            payload = {"model": model.model, "messages": [{"role": "user", "content": "Continue"}]}
            wrong_model = await self.client.post(inference, headers=bearer, json={**payload, "model": "other"})
            self.assertEqual(wrong_model.status_code, 403, wrong_model.text)
            # 首次推理可能发生在一次长工具调用之后，无需心跳来维持授权。
            now += 119 * 60
            correct = await self.client.post(inference, headers=bearer, json=payload)
            self.assertEqual(correct.status_code, 200, correct.text)
            now += 3 * 60 - 1
            before_deadline = await self.client.post(inference, headers=bearer, json=payload)
            self.assertEqual(before_deadline.status_code, 200, before_deadline.text)

            clock.time.side_effect = iter([now, now + 1, now + 1])
            at_deadline = await self.client.post(inference, headers=bearer, json=payload)
            self.assertEqual(at_deadline.status_code, 401, at_deadline.text)

            now += 1
            clock.time.side_effect = lambda: now
            past_deadline = await self.client.post(inference, headers=bearer, json=payload)
            self.assertEqual(past_deadline.status_code, 401, past_deadline.text)

    async def test_running_task_can_refresh_an_expired_token_without_resetting_its_budget(self):
        async with self.sessions() as db:
            model = LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1")
            agent = Agent(name="Task", mode="task", model=model.name, workspace_path=str(self.fixture_root / "refresh"))
            db.add_all([model, agent])
            await db.commit()

        files = {}
        now = 1_790_000_000
        def launch(run_id, workspace, env, *, task_files, **options):
            files.update(task_files)
            return "fixture-container"

        with (
            patch.object(settings, "diagent_task_model_proxy", True),
            patch.object(settings, "diagent_task_model_refresh", True),
            patch.object(settings, "access_token", "fixture-admin"),
            patch.object(settings, "workspace_root", self.fixture_root),
            patch("app.services.model_proxy.time") as clock,
            patch("app.services.model_proxy.MAX_TASK_MODEL_CALLS", 2),
            patch("app.services.docker_runner.start_container", side_effect=launch),
            patch("app.api.internal.model_proxy.internal_client", side_effect=lambda **opts: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": []})), **opts)),
        ):
            clock.time.side_effect = lambda: now
            admin = {"Authorization": "Bearer fixture-admin"}
            path = f"/api/os/a2a/agents/{agent.id}"
            sent = await self.client.post(path, headers=admin, json={
                "jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Continue"}]}},
            })
            task = sent.json()["result"]
            self.assertEqual(task["status"]["state"], "working", sent.text)
            self.assertIn("model-refresh.json", files)
            refresh = {"Authorization": "Bearer " + files["model-refresh.json"]["refresh_token"]}
            initial = files["task.json"]["models"]["models"][model.name]["api_key"]
            async def infer(token):
                return await self.client.post("/api/internal/model-proxy/v1/chat/completions",
                    headers={"Authorization": "Bearer " + token}, json={"model": model.model, "messages": []})
            refresh_path = "/api/internal/model-proxy/v1/grants/refresh"
            self.assertEqual((await infer(initial)).status_code, 200)
            self.assertEqual((await self.client.post(refresh_path, headers={"Authorization": "Bearer " + initial})).status_code, 401)
            now += 301
            self.assertEqual((await infer(initial)).status_code, 401)
            exchanged = await self.client.post(refresh_path, headers=refresh)
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            renewed = exchanged.json()["access_token"]
            self.assertNotEqual(renewed, initial)
            self.assertEqual((await infer(initial)).status_code, 401)
            self.assertEqual((await infer(renewed)).status_code, 200)
            self.assertEqual((await infer(renewed)).status_code, 429)
            canceled = await self.client.post(path, headers=admin, json={
                "jsonrpc": "2.0", "id": 2, "method": "tasks/cancel", "params": {"id": task["id"]},
            })
            self.assertEqual(canceled.json()["result"]["status"]["state"], "canceled")
            self.assertEqual((await self.client.post(refresh_path, headers=refresh)).status_code, 401)

    async def test_canceling_during_task_container_launch_cannot_reactivate_the_grant(self):
        async with self.sessions() as db:
            model = LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1")
            agent = Agent(name="Task", mode="task", model=model.name, workspace_path=str(self.fixture_root / "agent"))
            db.add_all([model, agent])
            await db.commit()

        entered = asyncio.Event()
        release_launch = threading.Event()
        loop = asyncio.get_running_loop()
        launch = {}

        def start_container(run_id, workspace, env, *, task_files, **options):
            launch.update(task_files["task.json"]["models"]["models"][model.name])
            loop.call_soon_threadsafe(entered.set)
            if not release_launch.wait(10):
                raise RuntimeError("Fixture did not release container launch")
            return "fixture-container"

        with (
            patch.object(settings, "diagent_task_model_proxy", True),
            patch.object(settings, "diagent_service_model_proxy", False),
            patch.object(settings, "access_token", "fixture-admin"),
            patch.object(settings, "workspace_root", self.fixture_root),
            patch("app.services.docker_runner.start_container", side_effect=start_container),
            patch("app.services.docker_runner.remove_container", return_value=True) as remove,
        ):
            admin = {"Authorization": "Bearer fixture-admin"}
            path = f"/api/os/a2a/agents/{agent.id}"
            sending = asyncio.create_task(self.client.post(path, headers=admin, json={
                "jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Continue"}]}},
            }))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                tasks = await self.client.get(f"/api/os/a2a/tasks?agent_id={agent.id}", headers=admin)
                task_id = tasks.json()["items"][0]["id"]
                changed = await self.client.put(f"/api/os/agents/{agent.id}", headers=admin, json={"mode": "service"})
                self.assertEqual(changed.status_code, 409, changed.text)
                chat = await self.client.post("/api/apps/chat/completions", headers=admin, json={
                    "agent_id": agent.id, "messages": [{"role": "user", "content": "Continue"}],
                })
                self.assertEqual(chat.status_code, 503, chat.text)
                self.assertIn("service-mode Agent", chat.text)
                self.assertFalse((self.fixture_root / "agent" / "models.yaml").exists())
                canceled = await self.client.post(path, headers=admin, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tasks/cancel", "params": {"id": task_id},
                })
                self.assertEqual(canceled.json()["result"]["status"]["state"], "canceled", canceled.text)
                docker = Mock()
                docker.containers.get.side_effect = NotFound("No service container")
                docker.containers.list.return_value = [Mock(status="running")]
                with patch("app.services.docker_runner._client", docker):
                    deleted = await self.client.delete(f"/api/os/agents/{agent.id}", headers=admin)
                self.assertEqual(deleted.status_code, 409, deleted.text)
                docker.containers.list.assert_called_once_with(all=True, filters={"label": f"dios.task-agent={agent.id}"})
            finally:
                release_launch.set()
                sent = await sending
            self.assertEqual(sent.json()["result"]["status"]["state"], "canceled", sent.text)
            remove.assert_called_once_with("fixture-container")
            replay = await self.client.post("/api/internal/model-proxy/v1/chat/completions", headers={
                "Authorization": "Bearer " + launch["api_key"],
            }, json={"model": model.model, "messages": []})
            self.assertEqual(replay.status_code, 401, replay.text)

    async def test_agent_api_rejects_unknown_tools_without_altering_saved_selection(self):
        with patch.object(settings, "diagent_service_model_proxy", True):
            created = await self.client.post("/api/os/agents", json={
                "name": "Text only", "workspace_path": str(self.fixture_root / "text-only"),
                "capabilities": {"diagent_tools": []},
            })
            self.assertEqual(created.status_code, 201, created.text)
            agent_path = f"/api/os/agents/{created.json()['id']}"
            rejected = await self.client.put(agent_path, json={"capabilities": {"diagent_tools": ["arbitrary-plugin"]}})
            self.assertEqual(rejected.status_code, 422, rejected.text)
            persisted = await self.client.get(agent_path)
            self.assertEqual(persisted.json()["capabilities"]["diagent_tools"], [])

    async def test_agent_api_requires_a_registered_streamable_http_server_for_explicit_mcp(self):
        async with self.sessions() as db:
            ledger = McpServer(name="ledger", transport="streamable_http", url="https://mcp.test/gateway")
            stdio = McpServer(name="local", transport="stdio", command="test-mcp")
            db.add_all([ledger, stdio])
            await db.commit()
        with patch.object(settings, "diagent_service_model_proxy", True):
            agent = {
                "name": "Reader", "workspace_path": str(self.fixture_root / "reader"),
                "capabilities": {"diagent_tools": ["mcp"], "governance": {"allowed_tools": ["lookup"]}},
            }
            for server_ids in (["unknown"], [stdio.id]):
                with self.subTest(server_ids=server_ids):
                    rejected = await self.client.post("/api/os/agents", json={**agent, "mcp_server_ids": server_ids})
                    self.assertEqual(rejected.status_code, 422, rejected.text)
            created = await self.client.post("/api/os/agents", json={**agent, "mcp_server_ids": [ledger.id]})
            self.assertEqual(created.status_code, 201, created.text)
            agent_path = f"/api/os/agents/{created.json()['id']}"
            for server_ids in ([], ["unknown"], [stdio.id]):
                with self.subTest(server_ids=server_ids):
                    rejected = await self.client.put(agent_path, json={"mcp_server_ids": server_ids})
                    self.assertEqual(rejected.status_code, 422, rejected.text)
            persisted = await self.client.get(agent_path)
            self.assertEqual(persisted.json()["mcp_server_ids"], [ledger.id])

    async def test_service_a2a_resolves_saved_model_references_and_issues_only_selected_mcp_grants(self):
        async with self.sessions() as db:
            model = LLMModel(name="fixture", model="fixture-model", base_url="https://model.test/v1")
            other_model = LLMModel(name="other", model="other-model", base_url="https://other.test/v1")
            ledger = McpServer(name="ledger", transport="streamable_http", url="https://mcp.test/gateway")
            db.add_all([model, other_model, ledger])
            await db.flush()
            governance = {"allowed_tools": ["lookup"]}
            reader = Agent(name="Reader", model=model.id, mcp_server_ids=[ledger.id],
                           capabilities={"diagent_tools": ["mcp"], "governance": governance})
            writer = Agent(name="Writer", model=model.name, mcp_server_ids=[ledger.id],
                           capabilities={"diagent_tools": [], "governance": governance})
            reporter = Agent(name="Reporter", model=model.model)
            db.add_all([reader, writer, reporter])
            await db.commit()

        admin_headers = {"Authorization": "Bearer fixture-admin"}
        forwarded = {}

        async def upstream(request):
            agent_id = request.url.host.removeprefix("diagent-")
            checked = await self.client.post("/api/internal/model-proxy/v1/grants/check", headers={
                "Authorization": "Bearer " + request.headers["X-DiAgent-Model-Grant"],
            }, json={"agent_id": agent_id, "model": "fixture-model"})
            self.assertEqual(checked.status_code, 200 if agent_id == reader.id else 204, checked.text)
            servers = list(checked.json()["mcp_servers"]) if checked.status_code == 200 else []
            payload = json.loads(request.content)
            forwarded[agent_id] = (payload.get("model"), payload.get("tool_selection"), servers)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Accepted"}}]})

        async def send_and_wait(agent):
            sent = await self.client.post(f"/api/os/a2a/agents/{agent.id}", headers=admin_headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "message/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Continue"}]}},
            })
            task_id = sent.json()["result"]["id"]
            async with asyncio.timeout(20):
                while True:
                    task = (await self.client.get(f"/api/os/a2a/tasks/{task_id}", headers=admin_headers)).json()
                    if task["status"] not in ("submitted", "working"):
                        self.assertEqual(task["status"], "completed", task)
                        return

        with (
            patch.object(settings, "diagent_service_model_proxy", True),
            patch.object(settings, "access_token", "fixture-admin"),
            patch("app.db.database.async_session", self.sessions),
            patch("app.services.agent_runtime.ensure_running",
                  new=AsyncMock(side_effect=lambda agent_id, _: f"http://diagent-{agent_id}")),
            patch("app.services.http_internal.internal_client",
                  side_effect=lambda **opts: httpx.AsyncClient(transport=httpx.MockTransport(upstream), **opts)),
        ):
            await send_and_wait(reader)
            await send_and_wait(writer)
            await send_and_wait(reporter)

        self.assertEqual(forwarded, {
            reader.id: ("fixture-model", {"tool_ids": ["mcp"]}, ["ledger"]),
            writer.id: ("fixture-model", {"tool_ids": []}, []),
            reporter.id: ("fixture-model", None, []),
        })

    async def test_managed_mcp_filters_discovery_and_revokes_with_the_turn(self):
        calls = []
        original_client = httpx.AsyncClient

        def upstream(request):
            if request.method == "DELETE":
                calls.append(("DELETE", request.headers.get("authorization")))
                self.assertEqual(request.headers.get("mcp-session-id"), "fixture-upstream-session")
                return httpx.Response(204)
            request_body = json.loads(request.content)
            calls.append((request_body["method"], request.headers.get("authorization")))
            if request_body["method"] == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": request_body["id"], "result": {}},
                                      headers={"Mcp-Session-Id": "fixture-upstream-session"})
            result = ({"tools": [{"name": "lookup"}, {"name": "remove"}]}
                      if request_body["method"] == "tools/list" else {"content": [{"type": "text", "text": "fixture-result"}]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": request_body["id"], "result": result})

        async with self.sessions() as db:
            ledger = McpServer(name="ledger", transport="streamable_http", url="https://mcp.test/gateway",
                               headers={"Authorization": "Bearer fixture-mcp-provider"})
            model = LLMModel(name="reviewer", model="fixture-model", base_url="https://model.test/v1")
            db.add_all([ledger, model])
            await db.flush()
            agent = Agent(name="Reviewer", model=model.id, mcp_server_ids=[ledger.id],
                          capabilities={"governance": {"allowed_tools": ["lookup"]}})
            db.add(agent)
            await db.commit()
            with (
                patch.object(settings, "diagent_service_model_proxy", True),
                patch.object(settings, "access_token", "fixture-admin"),
                patch("app.api.internal.e2ag_mcp.httpx.AsyncClient",
                      side_effect=lambda **opts: original_client(transport=httpx.MockTransport(upstream), **opts)),
            ):
                async with model_grant(db, agent, model, include_mcp=True) as headers:
                    checked = await self.client.post("/api/internal/model-proxy/v1/grants/check",
                                                     headers={"Authorization": "Bearer " + headers["X-DiAgent-Model-Grant"]},
                                                     json={"agent_id": agent.id, "model": model.model})
                    self.assertEqual(checked.status_code, 200)
                    self.assertNotIn("fixture-mcp-provider", checked.text)
                    names = checked.json()["mcp_servers"]
                    self.assertEqual(list(names), ["ledger"])
                    tool_grant = names["ledger"]
                    tool_url = "/api/internal/e2ag/mcp/" + tool_grant["grant_id"]
                    tool_headers = {"Authorization": "Bearer " + tool_grant["token"]}
                    self.assertNotEqual(tool_grant["token"], headers["X-DiAgent-Model-Grant"])
                    command = {"jsonrpc": "2.0", "id": 1}
                    self.assertEqual((await self.client.delete(tool_url, headers=tool_headers)).status_code, 405)
                    initialized = await self.client.post(tool_url, headers=tool_headers, json={**command, "method": "initialize"})
                    self.assertEqual(initialized.headers["mcp-session-id"], "fixture-upstream-session")
                    forged = await self.client.post(tool_url, headers={**tool_headers, "Mcp-Session-Id": "other-session"},
                                                    json={**command, "method": "tools/list"})
                    self.assertEqual(forged.status_code, 403)
                    tool_headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
                    self.assertEqual((await self.client.get(tool_url, headers=tool_headers)).status_code, 405)
                    discovery = await self.client.post(tool_url, headers=tool_headers, json={**command, "method": "tools/list"})
                    self.assertEqual(discovery.json()["result"]["tools"], [{"name": "lookup"}])
                    denied = await self.client.post(tool_url, headers=tool_headers, json={
                        **command, "method": "tools/call", "params": {"name": "remove"},
                    })
                    self.assertEqual(denied.status_code, 403)
                    allowed = await self.client.post(tool_url, headers=tool_headers, json={
                        **command, "method": "tools/call", "params": {"name": "lookup"},
                    })
                    self.assertEqual(allowed.json()["result"]["content"][0]["text"], "fixture-result")
                    with patch.object(settings, "diagent_service_model_proxy", False):
                        switched_off = await self.client.post(tool_url, headers=tool_headers,
                                                              json={**command, "method": "tools/list"})
                        self.assertEqual(switched_off.status_code, 403)
                revoked = await self.client.post(tool_url, headers=tool_headers, json={**command, "method": "tools/list"})
                self.assertEqual(revoked.status_code, 403)
        self.assertEqual(calls, [(method, "Bearer fixture-mcp-provider")
                                 for method in ("initialize", "tools/list", "tools/call", "DELETE")])

    async def test_inference_grant_only_spends_its_own_model_budget(self):
        path = "/api/internal/model-proxy/v1/chat/completions"
        payload = {"model": "fixture-model", "messages": [{"role": "user", "content": "Review"}], "stream": True}
        with patch.object(settings, "diagent_service_model_proxy", True), patch.object(settings, "access_token", "fixture-admin"):
            denied = await self.client.post(path, json=payload, headers={"Authorization": "Bearer fixture-admin"})
            self.assertEqual(denied.status_code, 401)

            calls = []

            def upstream(request):
                calls.append(request)
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b"data: [DONE]\n\n")

            async with self.sessions() as db:
                agent = Agent(name="Reviewer", model="reviewer")
                model = LLMModel(name="reviewer", model="fixture-model", base_url="https://model.test/v1", api_key="fixture-provider")
                db.add_all([agent, model])
                await db.commit()
                with (
                    patch("app.services.model_proxy.MAX_MODEL_CALLS", 1),
                    patch("app.api.internal.model_proxy.internal_client", side_effect=lambda **opts: httpx.AsyncClient(transport=httpx.MockTransport(upstream), **opts)),
                ):
                    async with model_grant(db, agent, model) as headers:
                        authorization = {"Authorization": f"Bearer {headers['X-DiAgent-Model-Grant']}"}
                        check_path = "/api/internal/model-proxy/v1/grants/check"
                        wrong_agent = await self.client.post(check_path, json={"model": model.model, "agent_id": "other-agent"}, headers=authorization)
                        self.assertEqual(wrong_agent.status_code, 403)
                        right_agent = await self.client.post(check_path, json={"model": model.model, "agent_id": agent.id}, headers=authorization)
                        self.assertEqual(right_agent.status_code, 204)
                        cross_model = await self.client.post(path, json={**payload, "model": "other-model"}, headers=authorization)
                        self.assertEqual(cross_model.status_code, 403)
                        results = await asyncio.gather(*(
                            self.client.post(path, json={**payload, "max_tokens": 100_000}, headers=authorization)
                            for _ in range(2)
                        ))
                        self.assertEqual(sorted(response.status_code for response in results), [200, 429])
                        self.assertEqual(next(response.text for response in results if response.status_code == 200), "data: [DONE]\n\n")
                    revoked = await self.client.post(path, json=payload, headers=authorization)
                    self.assertEqual(revoked.status_code, 401)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].headers["authorization"], "Bearer fixture-provider")
            self.assertEqual(str(calls[0].url), "https://model.test/v1/chat/completions")
            self.assertEqual(json.loads(calls[0].content)["max_tokens"], 100_000)

    async def test_inference_proxy_leaves_output_budget_unset_when_omitted(self):
        async with self.sessions() as db:
            agent = Agent(name="Reviewer", model="reviewer")
            model = LLMModel(name="reviewer", model="fixture-model", base_url="https://model.test/v1")
            db.add_all([agent, model])
            await db.commit()

            forwarded = []

            def upstream(request):
                forwarded.append(json.loads(request.content))
                return httpx.Response(200, json={"choices": []})

            with (
                patch.object(settings, "diagent_service_model_proxy", True),
                patch.object(settings, "access_token", "fixture-admin"),
                patch("app.api.internal.model_proxy.internal_client",
                      side_effect=lambda **opts: httpx.AsyncClient(transport=httpx.MockTransport(upstream), **opts)),
            ):
                async with model_grant(db, agent, model) as headers:
                    response = await self.client.post(
                        "/api/internal/model-proxy/v1/chat/completions",
                        json={"model": model.model, "messages": []},
                        headers={"Authorization": f"Bearer {headers['X-DiAgent-Model-Grant']}"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(forwarded, [{"model": model.model, "messages": []}])

    async def test_turn_grant_is_revoked_during_stream_cancellation(self):
        with patch.object(settings, "diagent_service_model_proxy", True), patch.object(settings, "access_token", "fixture-admin"):
            async with self.sessions() as db:
                agent = Agent(name="Reviewer", model="reviewer")
                model = LLMModel(name="reviewer", model="fixture-model", base_url="https://model.test/v1")
                db.add_all([agent, model])
                await db.commit()
                with anyio.CancelScope() as scope:
                    async with model_grant(db, agent, model):
                        scope.cancel()
                        await anyio.sleep(0)
            async with self.sessions() as db:
                self.assertIsNone(await db.scalar(select(ModelInferenceGrant.token_hash)))

    async def test_managed_runtime_does_not_mount_or_set_provider_credentials(self):
        with (
            patch.object(settings, "diagent_service_model_proxy", True),
            patch.object(settings, "access_token", "fixture-admin"),
            patch.object(settings, "workspace_root", self.fixture_root),
            patch("app.services.agent_runtime.get_client") as docker,
            patch("app.services.agent_runtime._health_check", return_value=True),
        ):
            docker.return_value.containers.get.side_effect = NotFound("fixture")
            docker.return_value.containers.run.return_value = Mock(id="test-container", short_id="test")
            workspace = self.fixture_root / "reviewer"
            installed_skill = self.fixture_root / "skills" / "review-skill"
            installed_skill.mkdir(parents=True)
            (installed_skill / "SKILL.md").write_text("Review instructions")
            unassigned = self.fixture_root / "skills" / "other-skill"
            unassigned.mkdir()
            (unassigned / "SKILL.md").write_text("Other instructions")
            async with self.sessions() as db:
                model = LLMModel(name="reviewer", model="fixture-model", base_url="https://model.test/v1", api_key="fixture-provider")
                agent = Agent(name="Reviewer", model="reviewer", workspace_path=str(workspace), skills=["review-skill", "inline-skill"], env={
                    "OPENAI_API_KEY": "legacy-key", "DIOS_TOKEN_ALIAS": " fixture-admin ",
                })
                inline_skill = Skill(name="inline-skill", description="Inline review instructions", content="Inline fixture instructions")
                db.add_all([agent, model, inline_skill])
                await db.commit()
                await ensure_running(agent.id, db)
                (workspace / "models.yaml").write_text("legacy fixture")
                with self.assertRaisesRegex(RuntimeError, "Move legacy models.yaml"):
                    await ensure_running(agent.id, db)
                (workspace / "models.yaml").unlink()
                db.add(Agent(name="Task Agent", mode="task", workspace_path=str(workspace / "nested")))
                await db.commit()
                with self.assertRaisesRegex(RuntimeError, "non-overlapping"):
                    await ensure_running(agent.id, db)
            self.assertFalse((workspace / "models.yaml").exists())
            env = docker.return_value.containers.run.call_args.kwargs["environment"]
            self.assertEqual(docker.return_value.containers.run.call_args.kwargs["name"], f"dios-agent-{agent.id}")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("LLM_MODELS_CONFIG_PATH", env)
            self.assertNotIn("DIOS_TOKEN_ALIAS", env)
            self.assertTrue(env["DIAGENT_MODEL_PROXY_URL"].endswith("/api/internal/model-proxy/v1"))
            self.assertNotIn("fixture-provider", str(env))
            self.assertEqual(json.loads(env["DIAGENT_SKILL_NAMES"]), ["review-skill", "inline-skill"])
            volumes = docker.return_value.containers.run.call_args.kwargs["volumes"]
            self.assertEqual(volumes[str(installed_skill)], {"bind": "/workspace/skills/review-skill", "mode": "ro"})
            inline_paths = [Path(path) for path, mount in volumes.items() if mount == {
                "bind": "/workspace/skills/inline-skill", "mode": "ro",
            }]
            self.assertEqual(len(inline_paths), 1)
            self.assertIn("Inline fixture instructions", (inline_paths[0] / "SKILL.md").read_text())
            self.assertNotIn(str(unassigned), volumes)
            self.assertNotIn(str(installed_skill.parent), volumes)

    async def test_agent_workspace_cannot_change_while_its_container_is_live(self):
        with (
            patch.object(settings, "diagent_service_model_proxy", True),
            patch.object(settings, "access_token", "fixture-admin"),
            patch("app.services.agent_runtime.get_client") as docker,
        ):
            async with self.sessions() as db:
                agent = Agent(name="Reviewer", workspace_path=str(self.fixture_root / "reviewer"))
                db.add(agent)
                await db.flush()
                db.add(AgentRuntime(agent_id=agent.id, container_id="live", url="http://reviewer", status="running"))
                await db.commit()
            docker.return_value.containers.get.return_value = Mock(status="running")
            path = f"/api/os/agents/{agent.id}"
            headers = {"Authorization": "Bearer fixture-admin"}
            moved = await self.client.put(path, json={"workspace_path": str(self.fixture_root / "other")}, headers=headers)
            self.assertEqual(moved.status_code, 409)
            removed = await self.client.delete(path, headers=headers)
            self.assertEqual(removed.status_code, 409)
            docker.return_value.containers.get.return_value.status = "exited"
            moved = await self.client.put(path, json={"workspace_path": str(self.fixture_root / "other")}, headers=headers)
            self.assertEqual(moved.status_code, 200)

    async def test_assigned_skill_cannot_be_removed_or_renamed(self):
        async with self.sessions() as db:
            skill = Skill(name="inline-skill", content="Review instructions")
            agent = Agent(name="Reviewer", skills=[skill.name])
            db.add_all([skill, agent])
            await db.commit()
        path = f"/api/os/skills/{skill.id}"
        headers = {"Authorization": "Bearer fixture-admin"}
        with patch.object(settings, "diagent_service_model_proxy", True), patch.object(settings, "access_token", "fixture-admin"):
            self.assertEqual((await self.client.delete(path, headers=headers)).status_code, 409)
            renamed = await self.client.put(path, json={"name": "renamed-skill"}, headers=headers)
            self.assertEqual(renamed.status_code, 409)

    async def test_task_root_skill_cannot_be_removed_or_renamed_until_disabled(self):
        async with self.sessions() as db:
            skill = Skill(name="task-review-skill", content="Review instructions")
            agent = Agent(name="Task reviewer", mode="task", skills=[skill.name], capabilities={"diagent_tools": ["skill"]})
            db.add_all([skill, agent])
            await db.commit()
        path = f"/api/os/skills/{skill.id}"

        renamed = await self.client.put(path, json={"name": "renamed-skill"})
        self.assertEqual(renamed.status_code, 409, renamed.text)
        deleted = await self.client.delete(path)
        self.assertEqual(deleted.status_code, 409, deleted.text)

        agent_path = f"/api/os/agents/{agent.id}"
        defaults = await self.client.put(agent_path, json={"capabilities": {}})
        self.assertEqual(defaults.status_code, 200, defaults.text)
        self.assertEqual((await self.client.delete(path)).status_code, 409)

        disabled = await self.client.put(agent_path, json={"capabilities": {"diagent_tools": []}})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual((await self.client.delete(path)).status_code, 204)
