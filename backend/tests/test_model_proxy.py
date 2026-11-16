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
from app.models.tables import Agent, AgentRuntime, LLMModel, ModelInferenceGrant
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
            async with self.sessions() as db:
                model = LLMModel(name="reviewer", model="fixture-model", base_url="https://model.test/v1", api_key="fixture-provider")
                agent = Agent(name="Reviewer", model="reviewer", workspace_path=str(workspace), env={
                    "OPENAI_API_KEY": "legacy-key", "DIOS_TOKEN_ALIAS": " fixture-admin ",
                })
                db.add_all([agent, model])
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
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("LLM_MODELS_CONFIG_PATH", env)
            self.assertNotIn("DIOS_TOKEN_ALIAS", env)
            self.assertTrue(env["DIAGENT_MODEL_PROXY_URL"].endswith("/api/internal/model-proxy/v1"))
            self.assertNotIn("fixture-provider", str(env))

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
