"""Durable execution associations, bounded cursors and current object access."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy import select
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.objects import Objects, reference
from app.runtime.schema import attempts
from app.services.run_evidence import RunEvidence
from app.services.task_console import TaskConsole
from app.services.knowledge import ObjectKnowledgeProvider
import test_runtime_tool_invocations as tools
import test_runtime_object_access as access


class EvidenceScenarios:
    postgres = False

    async def asyncSetUp(self):
        self.f = tools.ToolInvocationTests()
        if self.postgres:
            self.f.database_url = os.environ["DIOS_TEST_POSTGRES_URL"]
            self.f.postgres = True
        await self.f.asyncSetUp()
        f = self.f
        self.console = TaskConsole(
            f.db, f.scope, f.root / "profiles", f.provider.root, policy=f.policy
        )
        self.viewer = RunEvidence(self.console)

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    async def artifact(self, value=b"retained evidence"):
        blob = self.f.provider.put([value])
        return await self.f.store.retain_output(
            self.f.binding, self.f.provider, blob.key, "stdout"
        )

    async def test_revision_is_historical_and_contains_no_runtime_secrets(self):
        from app.runtime.execution_revisions import compile_profile
        from app.runtime.revisions import Revisions

        raw = dict(
            id="historical",
            bridge="jsonl-v1",
            governance={"mode": "trusted-local"},
            launch=dict(image="sha256:" + "a" * 64, command=["private-command"]),
        )
        record = await Revisions(self.f.db, self.f.scope).create(
            "revision", compile_profile(raw), actor="operator"
        )
        value = record["revision"]
        from app.runtime.worker import read_profile

        _, spec = read_profile("unused", value=raw)
        run = await self.f.store.submit(
            "historical",
            dict(
                runtime_profile="historical",
                profile_digest=spec.fingerprint(),
                secret_refs=[],
            ),
            revision_ref=value["id"],
        )
        with patch(
            "app.runtime.execution_revisions.adapter_descriptor",
            side_effect=RuntimeError("new adapter"),
        ):
            result = await self.viewer.view(run["id"])
        self.assertEqual(result["root"]["execution"]["digest"], value["content_digest"])
        self.assertEqual(result["root"]["execution"]["status"], "recorded")
        self.assertNotIn("private-command", json.dumps(result))
        self.assertNotIn("secret_refs", json.dumps(result))
        self.assertEqual(
            (await self.viewer.view(self.f.run["id"]))["root"]["execution"]["status"],
            "not_recorded",
        )

    async def test_knowledge_replacement_keeps_explicit_source_version_and_nested_links(
        self,
    ):
        catalog = Objects(self.f.db, self.f.scope)
        file = await catalog.publish(
            self.f.provider,
            ["knowledge source version one".encode()],
            "source",
            "operator",
            media_type="text/plain",
        )
        source = ObjectKnowledgeProvider(self.console)
        provenance = dict(
            namespace="knowledge", key="manual", version="v1", locator="chapter-1"
        )
        await source.change(
            "manual",
            dict(
                expected_revision=0,
                idempotency_key="bind",
                object_ref=file["ref"],
                provenance=provenance,
                expires_at=None,
                state="active",
            ),
        )
        found = await source.search("knowledge")
        self.assertEqual(found["items"][0]["object_ref"], file["ref"])
        aid = await self.artifact()
        await catalog.link(
            reference("artifact", aid), "derived_from", file["ref"], "operator"
        )
        await catalog.link(
            file["ref"],
            "associated_with",
            {k: provenance[k] for k in ("namespace", "key", "version")},
            "operator",
        )
        second = await catalog.publish(
            self.f.provider,
            [b"replacement"],
            "source-next",
            "operator",
            object_id=file["ref"]["id"],
            expected_version=1,
        )
        await source.change(
            "manual",
            dict(
                expected_revision=1,
                idempotency_key="replace",
                object_ref=second["ref"],
                provenance=dict(provenance, version="v2"),
                expires_at=None,
                state="active",
            ),
        )
        result = await self.viewer.view(self.f.run["id"], section="sources")
        self.assertEqual(result["items"][0]["target"], file["ref"])
        self.assertEqual(result["items"][0]["descriptor"], file["descriptor"])
        self.assertEqual(result["items"][0]["evidence_level"], "operator_declared")
        self.assertEqual(result["coverage"]["source_usage"], "explicit_relations_only")
        self.assertEqual(
            (await self.viewer.reader.relations(file["ref"]))["items"][0]["target"][
                "version"
            ],
            "v1",
        )

    async def test_approval_unknown_receipt_and_usage_keep_distinct_grades(self):
        item = await self.f.request()
        pending = await self.viewer.view(self.f.run["id"], section="tools")
        self.assertEqual(pending["items"][0]["state"], "awaiting_approval")
        self.assertNotIn("arguments", pending["items"][0])
        self.assertNotIn("evidence", pending["items"][0])
        item = await self.f.approve(item)
        self.f.transport.failure = True
        await self.f.gate.dispatch(self.f.run["id"], item["id"], item["version"])
        unknown = (await self.viewer.view(self.f.run["id"], section="tools"))["items"][
            0
        ]
        self.assertEqual(unknown["state"], "result_unknown")
        self.assertEqual(unknown["result_evidence"], "unknown")
        usage = dict(
            type="usage",
            session_id="session-one",
            sequence=1,
            kind="acp_prompt",
            quality="reported",
            values=dict(inputTokens=20, outputTokens=3, totalTokens=23),
        )
        frames = [dict(type="session", session_id="session-one"), usage]
        await self.artifact(
            b"".join(json.dumps(row).encode() + b"\n" for row in frames)
        )
        reported = (await self.viewer.view(self.f.run["id"], section="usage"))["items"][
            0
        ]
        self.assertEqual(reported["evidence_level"], "agent_reported")
        self.assertEqual(reported["aggregation"], "not_additive")

    async def test_pagination_survives_restart_and_is_bound_to_run_section_scope(self):
        ids = [await self.artifact(str(n).encode()) for n in range(3)]
        result = await self.viewer.view(self.f.run["id"], section="artifacts", limit=1)
        seen = [result["items"][0]["id"]]
        cursor = result["next_cursor"]
        for section in ["tools", "sources"]:
            with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_CURSOR"):
                await self.viewer.view(self.f.run["id"], section=section, cursor=cursor)
        db = Database(self.f.url)
        try:
            viewer = RunEvidence(
                TaskConsole(
                    db, self.f.scope, self.f.root / "profiles", self.f.provider.root
                )
            )
            while cursor:
                result = await viewer.view(
                    self.f.run["id"], section="artifacts", limit=1, cursor=cursor
                )
                seen.extend(x["id"] for x in result["items"])
                cursor = result["next_cursor"]
            self.assertEqual(sorted(seen), sorted(ids))
            other = RunEvidence(
                TaskConsole(
                    db, "foreign", self.f.root / "profiles", self.f.provider.root
                )
            )
            with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
                await other.view(self.f.run["id"])
        finally:
            await db.close()

    async def test_cross_worker_attempts_and_remote_report_are_separate(self):
        from app.runtime.adapter import Observation

        f = self.f
        await f.store.observe_process(
            f.binding,
            Observation(
                f.attempt["id"],
                "exited",
                False,
                None,
                "process-one",
                1,
                "not_configured",
                "host",
            ),
            adapter="local-docker-v1",
            boot_id="boot-one",
        )
        await f.store.mark_unknown(f.binding, "PROCESS_EXITED")
        run = await f.store.get(f.run["id"])
        await f.store.reconcile_absent(run["id"], run["version"], "confirmed-exit")
        run = await f.store.get(f.run["id"])
        await f.store.retry(run["id"], run["version"])
        worker_id = "another-" + f.scope
        await f.store.register_worker(worker_id, "next-instance", "another-host", 123)
        new = await f.store.claim(worker_id, "next-instance")
        records = (await self.viewer.view(f.run["id"]))["items"]
        self.assertEqual([x["worker_id"] for x in records], [f.binding[2], worker_id])
        self.assertEqual(records[0]["evidence_level"], "worker_observed")
        self.assertEqual(records[1]["evidence_level"], "unknown")
        # Stored remote Task/Context is protocol evidence, not a native process observation.
        async with f.db.transaction(f.scope) as c:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == new["id"])
                .values(
                    session_id="remote-session",
                    remote={
                        "a2a": {
                            "task_id": "remote-task",
                            "context_id": "remote-context",
                            "observed_state": "working",
                        }
                    },
                )
            )
        remote = (await self.viewer.view(f.run["id"]))["items"][1]
        self.assertEqual(remote["evidence_level"], "remote_reported")
        self.assertEqual(remote["remote_task"]["task_id"], "remote-task")


class SQLiteEvidenceTests(EvidenceScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_actual_cli_and_sdk_http_share_query_and_pagination(self):
        from pathlib import Path
        import socket
        import subprocess
        import sys
        import threading
        import time
        import uvicorn
        from fastapi import FastAPI
        from app.api.os import runtime_tasks

        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "sdk/python/src"))
        from dios_sdk import TaskClient

        ids = [await self.artifact(str(n).encode()) for n in range(2)]
        process = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "app.runtime.cli",
                "--database-url",
                self.f.url,
                "--scope",
                self.f.scope,
                "evidence",
                self.f.run["id"],
                "--artifacts",
                str(self.f.provider.root),
                "--section",
                "artifacts",
            ],
            cwd=root / "backend",
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            sorted(row["id"] for row in json.loads(process.stdout)["items"]),
            sorted(ids),
        )
        app = FastAPI()
        app.state.runtime_console = self.console
        app.include_router(runtime_tasks.router, prefix="/api/os")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        with (
            patch.object(runtime_tasks.settings, "runtime_console_enabled", True),
            patch.object(runtime_tasks.settings, "access_token", "query-fixture"),
        ):
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                self.assertTrue(server.started)
                client = TaskClient(f"http://127.0.0.1:{port}", "query-fixture")
                first = await asyncio.to_thread(
                    client.evidence, self.f.run["id"], section="artifacts", limit=1
                )
                second = await asyncio.to_thread(
                    client.evidence,
                    self.f.run["id"],
                    section="artifacts",
                    limit=1,
                    cursor=first["next_cursor"],
                )
                self.assertEqual(
                    sorted([first["items"][0]["id"], second["items"][0]["id"]]),
                    sorted(ids),
                )
                self.assertIsNone(second["next_cursor"])
            finally:
                server.should_exit = True
                await asyncio.to_thread(thread.join, 5)
                sock.close()


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "Isolated PostgreSQL required"
)
class PostgresEvidenceTests(EvidenceScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class EvidenceAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = access.ObjectAccessTests.asyncSetUp
    asyncTearDown = access.ObjectAccessTests.asyncTearDown
    save = access.ObjectAccessTests.save
    headers_for = access.ObjectAccessTests.headers_for
    submit_as = access.ObjectAccessTests.submit_as
    get = access.ObjectAccessTests.get
    finish = access.ObjectAccessTests.finish
    publish = access.ObjectAccessTests.publish
    set_policy = access.ObjectAccessTests.set_policy
    path = access.ObjectAccessTests.path

    async def test_run_read_does_not_grant_tools_artifacts_or_sources(self):
        run = await self.submit_as("a")
        await self.finish()
        self.directory["principals"]["b"]["objects"][run["id"]] = ["run.read"]
        self.save()
        for section in ["tools", "artifacts", "sources", "quarantine", "transfers"]:
            value = await self.get("b", f"runs/{run['id']}/evidence?section={section}")
            self.assertEqual(value.status_code, 200, value.text)
            self.assertEqual(value.json()["access"], "restricted")
            self.assertEqual(value.json()["items"], [])
        self.assertEqual(
            (await self.get("c", f"runs/{run['id']}/evidence")).status_code, 404
        )
        self.directory["principals"]["b"]["objects"].clear()
        self.save()
        self.assertEqual(
            (await self.get("b", f"runs/{run['id']}/evidence")).status_code, 404
        )

    async def test_source_acl_session_acl_and_empty_scanned_page(self):
        run = await self.submit_as("a")
        await self.finish()
        detail = await self.console.detail(run["id"])
        artifact = detail["artifacts"][0]["object_ref"]
        session = detail["attempts"][0]["object_ref"]
        first = await self.publish("first")
        second = await self.publish("second")
        catalog = Objects(self.db, "one")
        links = [
            await catalog.link(artifact, "associated_with", ref, "operator")
            for ref in [first, second]
        ]
        links.sort(key=lambda x: x["id"])
        await self.set_policy(links[1]["target"], ["a"], key="visible")
        value = await self.get(
            "a", f"runs/{run['id']}/evidence?section=sources&limit=1"
        )
        self.assertEqual(value.status_code, 200, value.text)
        self.assertEqual(value.json()["items"], [])
        self.assertTrue(value.json()["next_cursor"])
        value = await self.client.get(
            f"/api/os/runtime/runs/{run['id']}/evidence",
            headers=self.headers_for("a"),
            params={
                "section": "sources",
                "cursor": value.json()["next_cursor"],
                "limit": 1,
            },
        )
        self.assertEqual(value.json()["items"][0]["target"], links[1]["target"])
        await self.set_policy(links[1]["target"], [], revision=1, key="revoke")
        self.assertEqual(
            (await self.get("a", f"runs/{run['id']}/evidence?section=sources")).json()[
                "items"
            ],
            [],
        )
        await self.set_policy(session, [], key="hide-session")
        value = (await self.get("a", f"runs/{run['id']}/evidence")).json()
        self.assertIsNone(value["items"][0]["session"])
        self.assertNotIn("fixture-session", json.dumps(value))

    async def test_token_revoked_during_query_cannot_return_pending_response(self):
        from app.services.run_evidence import RunEvidence

        run = await self.submit_as("a")
        await self.finish()
        original = RunEvidence.project

        async def revoke(viewer, section, row):
            value = await original(viewer, section, row)
            self.directory["principals"]["a"]["tokens"][0]["enabled"] = False
            self.save()
            return value

        with patch.object(RunEvidence, "project", revoke):
            response = await self.get("a", f"runs/{run['id']}/evidence")
        self.assertEqual(response.status_code, 401)
