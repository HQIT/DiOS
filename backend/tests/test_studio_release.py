"""Publication has one approval, one immutable version and one governed probe."""

import asyncio
import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

import test_runtime_task_console as fixtures
from app.api.os import configuration_studio as api
from app.runtime.database import RuntimeErrorCode
from app.runtime.deployments import Deployments
from app.runtime.execution_revisions import load_profile
from app.runtime.revision_schema import revisions
from app.runtime.studio_release_schema import releases
from app.runtime.tool_catalog import FileApprovers
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker, read_profile
from app.services.studio_release import StudioReleases
from work_auth_fixtures import policy_fixture


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = fixtures.TaskConsoleTests.asyncTearDown

    async def setup_release(self):
        self.raw = copy.deepcopy(self.profile)
        self.raw["governance"] = {"mode": "e2ag"}
        self.raw["launch"]["image"] = "sha256:" + "1" * 64
        self.raw["console"].pop("input_catalog")
        catalog = json.loads(self.input_catalog.read_text())
        catalog["result_schema"] = {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        }
        self.raw["console"]["input_catalog_data"] = catalog
        _, spec = read_profile("fixture", value=self.raw)
        self.console.policy, _ = policy_fixture(self.root, "review", spec)
        self.approver_path = self.root / "approvers.json"
        self.approver_value = {
            "version": 1,
            "approvers": [
                {
                    "subject": "reviewer:test",
                    "token_sha256": hashlib.sha256(b"reviewer-test").hexdigest(),
                    "enabled": True,
                    "scopes": ["one"],
                    "tools": ["studio.publish"],
                }
            ],
        }
        self.approver_path.write_text(json.dumps(self.approver_value))
        self.provider = FileApprovers(self.approver_path)
        self.manager = StudioReleases(self.console, self.provider)
        self.app.include_router(api.router, prefix="/api/os")
        self.body = {
            "base": json.dumps(self.raw),
            "draft": json.dumps(self.raw),
            "deployment_id": "primary",
            "deployment_version": 0,
            "idempotency_key": "first",
            "sample_id": "first-item",
        }

    async def request(self, **changes):
        body = dict(self.body, **changes)
        result = await self.client.post(
            "/api/os/runtime/studio/releases", json=body, headers=self.headers
        )
        self.assertEqual(result.status_code, 202, result.text)
        return result.json()

    async def approve(self, request):
        return await self.manager.decide(
            request["id"], request["version"], "approved", "reviewer-test", "verified"
        )

    async def finish_probe(self, published, *, fail=False):
        ref = published["detail"]["revision_ref"]
        _, profile, spec, _, _ = await load_profile(self.db, "one", ref)
        messages = [
            {"type": "session", "session_id": "release-session"},
            {
                "type": "result",
                "session_id": "release-session",
                "result": {"summary": "healthy", "evidence": ["fixture:source"]},
            },
        ]
        data = (
            b'{"type":"error","code":"EVIDENCE_INSUFFICIENT"}\n'
            if fail
            else ("\n".join(json.dumps(x) for x in messages) + "\n").encode()
        )
        worker = Worker(
            self.console.store,
            fixtures.FakeAdapter(data),
            profile,
            spec,
            self.console.provider,
            revision_ref=ref,
            authorization_verifier=WorkAuthorizationVerifier(
                self.console.store, self.console.policy
            ),
        )
        finished = await worker.once()
        if not fail:
            self.assertEqual(finished["state"], "succeeded", finished)
        return finished

    async def test_publication_is_atomic_idempotent_and_worker_gated(self):
        await self.setup_release()
        request = await self.request()
        self.assertEqual((await self.request())["id"], request["id"])
        with self.assertRaisesRegex(RuntimeErrorCode, "STUDIO_PROBE_NOT_READY"):
            await self.manager.probe(request["id"])
        with self.assertRaisesRegex(RuntimeErrorCode, "APPROVAL_REQUIRED"):
            await self.manager.publish(request["id"], 1, "operator:console")
        approved = await self.approve(request)
        self.assertEqual((await self.approve(request))["version"], 2)
        rows = await asyncio.gather(
            *(
                self.manager.publish(approved["id"], 2, "operator:console")
                for _ in range(3)
            )
        )
        self.assertEqual(len({row["detail"]["probe_run_id"] for row in rows}), 1)
        current = await Deployments(self.db, "one", self.console.policy).show("primary")
        self.assertIsNone(current["status"]["active_revision_id"])
        self.assertEqual(current["probe"]["state"], "queued")
        self.assertEqual((await self.finish_probe(rows[0]))["state"], "succeeded")
        probe_url = f"/api/os/runtime/studio/releases/{request['id']}/probe"
        self.assertEqual((await self.client.get(probe_url)).status_code, 401)
        probe = (await self.client.get(probe_url, headers=self.headers)).json()
        self.assertEqual(probe["state"], "succeeded")
        self.assertEqual(probe["revision_ref"], rows[0]["detail"]["revision_ref"])
        self.assertEqual(probe["authorization"]["mode"], "e2ag")
        self.assertNotIn("input", probe)
        current = await Deployments(self.db, "one", self.console.policy).reconcile(
            "primary"
        )
        self.assertEqual(
            current["status"]["active_revision_id"], rows[0]["detail"]["revision_ref"]
        )
        recovered = StudioReleases(self.console, self.provider)
        self.assertEqual((await recovered.show(request["id"]))["state"], "published")
        self.assertEqual(len(await recovered.list()), 1)
        async with self.db.read("one") as c:
            self.assertEqual(
                (
                    await c.execute(select(func.count()).select_from(revisions))
                ).scalar_one(),
                1,
            )
            self.assertEqual(
                (
                    await c.execute(select(func.count()).select_from(releases))
                ).scalar_one(),
                3,
            )
        with self.assertRaises(DBAPIError):
            async with self.db.transaction("one") as c:
                await c.execute(releases.update().values(state="approved"))

    async def test_http_auth_approval_reject_and_immutable_request(self):
        await self.setup_release()
        self.assertEqual(
            (
                await self.client.post(
                    "/api/os/runtime/studio/releases", json=self.body
                )
            ).status_code,
            401,
        )
        request = await self.request()
        with patch.object(
            api.settings, "runtime_approvers_file", str(self.approver_path)
        ):
            path = f"/api/os/runtime/studio/releases/{request['id']}"
            decision = {"expected_version": 1, "decision": "approved"}
            failed = await self.client.post(
                path + "/approval", json=decision, headers=self.headers
            )
            self.assertEqual(failed.json()["detail"], "APPROVER_UNAUTHENTICATED")
            decision["decision"] = "denied"
            denied = await self.client.post(
                path + "/approval",
                json=decision,
                headers=dict(
                    self.headers, **{"X-DiOS-Approver-Token": "reviewer-test"}
                ),
            )
            self.assertEqual(denied.status_code, 200, denied.text)
            denied = await self.client.post(
                path + "/publish", json={"expected_version": 2}, headers=self.headers
            )
            self.assertEqual(
                denied.json()["detail"], "STUDIO_RELEASE_APPROVAL_REQUIRED"
            )
            replacement = await self.client.post(
                path + "/publish",
                json={"expected_version": 2, "draft": "{}"},
                headers=self.headers,
            )
            self.assertEqual(replacement.status_code, 422)
        changed = dict(self.body, deployment_id="other")
        response = await self.client.post(
            "/api/os/runtime/studio/releases", json=changed, headers=self.headers
        )
        self.assertEqual(response.json()["detail"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(
            (await self.manager.show(request["id"]))["snapshot"]["draft"],
            self.body["draft"],
        )

    async def test_revocation_policy_change_and_independent_identity_block_publish(
        self,
    ):
        await self.setup_release()
        request = await self.request()
        self.approver_value["approvers"][0]["subject"] = "operator:test"
        self.approver_path.write_text(json.dumps(self.approver_value))
        with self.assertRaisesRegex(RuntimeErrorCode, "INDEPENDENT"):
            await self.approve(request)
        self.approver_value["approvers"][0]["subject"] = "reviewer:test"
        self.approver_path.write_text(json.dumps(self.approver_value))
        await self.approve(request)
        self.approver_value["approvers"][0]["enabled"] = False
        self.approver_path.write_text(json.dumps(self.approver_value))
        with self.assertRaisesRegex(RuntimeErrorCode, "APPROVER_FORBIDDEN"):
            await self.manager.publish(request["id"], 2, "operator:console")
        self.assertEqual(
            (await self.manager.show(request["id"]))["state"], "invalidated"
        )
        self.approver_value["approvers"][0]["enabled"] = True
        self.approver_path.write_text(json.dumps(self.approver_value))
        second = await self.request(idempotency_key="second")
        await self.approve(second)
        value = json.loads(self.console.policy.path.read_text())
        value["profiles"]["review"]["work_ttl_seconds"] = 120
        self.console.policy.path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeErrorCode, "STUDIO_POLICY_CHANGED"):
            await self.manager.publish(second["id"], 2, "operator:console")
        async with self.db.read("one") as c:
            self.assertEqual(
                (
                    await c.execute(select(func.count()).select_from(revisions))
                ).scalar_one(),
                0,
            )

    async def test_validation_and_stale_deployment_leave_no_revision_or_probe(self):
        await self.setup_release()
        for mutate, expected in [
            (
                lambda v: v["launch"].update(image="mutable:tag"),
                "REVISION_IMAGE_NOT_PINNED",
            ),
            (
                lambda v: v["console"].update(input_catalog="/etc/passwd"),
                "STUDIO_INLINE_INPUT_CATALOG_REQUIRED",
            ),
            (lambda v: v["launch"].update(cpu_count=0), "INVALID_STUDIO_DRAFT"),
        ]:
            raw = copy.deepcopy(self.raw)
            mutate(raw)
            response = await self.client.post(
                "/api/os/runtime/studio/releases",
                json=dict(self.body, draft=json.dumps(raw)),
                headers=self.headers,
            )
            self.assertEqual(response.json()["detail"], expected, response.text)
        request = await self.request(deployment_version=99)
        await self.approve(request)
        with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_CONFLICT"):
            await self.manager.publish(request["id"], 2, "operator:console")
        async with self.db.read("one") as c:
            self.assertEqual(
                (
                    await c.execute(select(func.count()).select_from(revisions))
                ).scalar_one(),
                0,
            )
        self.assertEqual((await self.manager.show(request["id"]))["state"], "approved")

    async def test_failure_keeps_active_version_and_previous_run_identity(self):
        await self.setup_release()
        first = await self.request()
        await self.approve(first)
        first = await self.manager.publish(first["id"], 2, "operator:console")
        run = await self.finish_probe(first)
        deployments = Deployments(self.db, "one", self.console.policy)
        ready = await deployments.reconcile("primary")
        changed = copy.deepcopy(self.raw)
        changed["launch"]["cpu_count"] = 2
        _, spec = read_profile("fixture", value=changed)
        prior = json.loads(self.console.policy.path.read_text())
        policy_fixture(self.root, "review", spec)
        policy = json.loads(self.console.policy.path.read_text())
        policy["profiles"]["review"]["revisions"] = {
            prior["profiles"]["review"]["profile_digest"]: prior["profiles"]["review"]
        }
        self.console.policy.path.write_text(json.dumps(policy))
        second = await self.request(
            draft=json.dumps(changed),
            idempotency_key="second",
            deployment_version=ready["deployment"]["version"],
        )
        await self.approve(second)
        second = await self.manager.publish(second["id"], 2, "operator:console")
        self.assertEqual(
            (await self.finish_probe(second, fail=True))["state"], "failed"
        )
        failed = await deployments.reconcile("primary")
        self.assertEqual(
            failed["status"]["active_revision_id"], first["detail"]["revision_ref"]
        )
        retained = await self.console.store.get(run["id"])
        self.assertEqual(retained["attempt_id"], run["attempt_id"])
        self.assertEqual(
            retained["input"]["revision_ref"], first["detail"]["revision_ref"]
        )

    async def test_expiry_cross_scope_and_migration_are_fail_closed(self):
        await self.setup_release()
        request = await self.request()
        other = StudioReleases(
            SimpleNamespace(db=self.db, scope="other", policy=self.console.policy),
            self.provider,
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await other.show(request["id"])
        self.assertEqual(await other.list(), [])
        with patch.object(
            self.db, "now", return_value=request["snapshot"]["expires_at"] + 1
        ):
            with self.assertRaisesRegex(RuntimeErrorCode, "APPROVAL_EXPIRED"):
                await self.approve(request)
        await self.approve(request)
        with patch.object(
            self.db, "now", return_value=request["snapshot"]["expires_at"] + 1
        ):
            with self.assertRaisesRegex(RuntimeErrorCode, "APPROVAL_EXPIRED"):
                await self.manager.publish(request["id"], 2, "operator:console")
        self.assertFalse((await self.db.migrate())["changed"])
        self.assertEqual((await self.manager.show(request["id"]))["state"], "approved")
