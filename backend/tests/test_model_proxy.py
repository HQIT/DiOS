"""验证模型代理与模型管理接口的凭据边界。"""

import asyncio
import json
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import anyio
import httpx
from docker.errors import NotFound
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.database import Base, get_db
from app.config import settings
from app.models.tables import Agent, AgentRuntime, LLMModel, McpServer, ModelInferenceGrant, Skill
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
            self.assertEqual(json.loads(calls[0].content)["max_tokens"], 4096)

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
