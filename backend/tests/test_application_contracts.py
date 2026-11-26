"""Application requirements are checked at immutable publication and execution admission."""

import copy
import json
import unittest
from unittest.mock import patch

import test_runtime_task_console as fixtures
import test_studio_release as release_fixtures
from app.api.os import application_contracts as api
from app.runtime.application_contracts import INTERFACES, compatibility, manifest
from app.runtime.database import RuntimeErrorCode
from app.runtime.execution_revisions import compile_profile
from app.runtime.revisions import Revisions
from app.runtime.store import digest
from app.runtime.worker import read_profile


class ApplicationContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = fixtures.TaskConsoleTests.asyncTearDown

    async def setup_contract(self):
        await release_fixtures.ReleaseTests.setup_release(self)
        self.app.include_router(api.router, prefix="/api/os")
        catalog = self.raw["console"]["input_catalog_data"]
        catalog.setdefault("schema", {"type": "object"})
        catalog["requires_e2ag"] = True
        self.contract = {
            "schema_version": 1,
            "id": "external-reference",
            "version": "1.0.0",
            "deployment": "independent",
            "requires": {**{k: ">=1,<2" for k in INTERFACES}, "worker-jsonl": ">=1,<2"},
            "input": {
                "kind": self.raw["console"]["input_kind"],
                "schema_digest": "sha256:" + digest(catalog["schema"]),
                "result_schema_digest": "sha256:" + digest(catalog["result_schema"]),
            },
        }
        self.raw["application_contract"] = self.contract
        self.revision = (
            await Revisions(self.db, "one").create(
                "app", compile_profile(self.raw), actor="operator"
            )
        )["revision"]["id"]
        self.profile_path = self.profiles / "review.json"
        self.profile_path.write_text(
            json.dumps({"id": "review", "revision_ref": self.revision, "enabled": True})
        )

    async def test_registered_revision_binds_app_input_result_and_history(self):
        await self.setup_contract()
        with patch(
            "importlib.import_module",
            side_effect=AssertionError("Application import forbidden"),
        ):
            run = await self.console.submit(
                "review", "first-item", "application-neutral", "app-task"
            )
        self.assertEqual(
            run["application"],
            {
                "id": "external-reference",
                "version": "1.0.0",
                "digest": "sha256:" + digest(self.contract),
            },
        )
        again = await self.console.submit(
            "review", "first-item", "application-neutral", "app-task"
        )
        self.assertEqual(run["id"], again["id"])
        self.profile_path.write_text(
            json.dumps(
                {"id": "review", "revision_ref": self.revision, "enabled": False}
            )
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "PROFILE_NOT_AVAILABLE"):
            await self.console.submit("review", "first-item", "new", "new")
        self.assertEqual(
            (await self.console.detail(run["id"]))["application"], run["application"]
        )

    async def test_incompatible_interfaces_and_schema_reject_before_publication(self):
        await self.setup_contract()
        for change, code in [
            ("api", "APPLICATION_INTERFACE_UNSUPPORTED"),
            ("bridge", "APPLICATION_INTERFACE_UNSUPPORTED"),
            ("schema", "APPLICATION_SCHEMA_BINDING_CHANGED"),
            ("governance", "APPLICATION_INTERFACE_UNSUPPORTED"),
        ]:
            candidate = copy.deepcopy(self.raw)
            if change == "api":
                candidate["application_contract"]["requires"]["runtime-api"] = ">=2"
            elif change == "bridge":
                candidate["application_contract"]["requires"][
                    "governed-tool-channel"
                ] = ">=1"
            elif change == "schema":
                candidate["console"]["input_catalog_data"]["schema"] = {
                    "type": "object",
                    "required": ["id"],
                }
            else:
                candidate["governance"] = {"mode": "none"}
            with self.assertRaisesRegex(RuntimeErrorCode, code):
                compile_profile(candidate)
        with self.assertRaisesRegex(RuntimeErrorCode, "APPLICATION_REVISION_REQUIRED"):
            read_profile("inline", value=self.raw)

    async def test_version_header_and_authenticated_preview_are_explicit(self):
        await self.setup_contract()
        self.assertEqual(
            (await self.client.get("/api/os/runtime/contracts")).status_code, 401
        )
        response = await self.client.get(
            "/api/os/runtime/contracts", headers=self.headers
        )
        self.assertEqual(response.headers["X-DiOS-Runtime-Contract"], "1")
        response = await self.client.post(
            "/api/os/runtime/runs",
            headers={**self.headers, "X-DiOS-Runtime-Contract": "2"},
            json={},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "RUNTIME_CONTRACT_UNSUPPORTED")
        self.app.dependency_overrides[api.operator] = lambda: {
            "id": "authenticated-principal"
        }
        try:
            discovery = await self.client.get("/api/os/runtime/contracts")
            self.assertEqual(discovery.status_code, 200)
            catalog = await self.client.get("/api/os/runtime/applications")
            self.assertEqual(catalog.status_code, 401)
        finally:
            self.app.dependency_overrides.pop(api.operator)
        candidate = copy.deepcopy(self.contract)
        candidate["requires"]["future-capability"] = ">=1"
        response = await self.client.post(
            "/api/os/runtime/applications/compatibility",
            headers=self.headers,
            json={"revision_ref": self.revision, "contract": candidate},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["compatible"])
        self.assertFalse(response.json()["registered_contract"])
        response = await self.client.get(
            "/api/os/runtime/applications", headers=self.headers
        )
        self.assertEqual(
            response.json()["items"][0]["application"]["id"], "external-reference"
        )
        self.assertEqual((await self.console.list())["items"], [])

    async def test_confirmed_cancellation_is_an_execution_requirement(self):
        await self.setup_contract()
        required = copy.deepcopy(self.contract)
        required["requires"]["confirmed-cancellation"] = ">=1,<2"
        managed = compatibility(self.raw, required)
        self.assertTrue(managed["compatible"])
        self.assertEqual(managed["interfaces"]["confirmed-cancellation"], "1.0")
        discovery = await self.client.get(
            "/api/os/runtime/contracts", headers=self.headers
        )
        capabilities = discovery.json()["execution_interfaces"]
        self.assertIn("confirmed-cancellation", capabilities["jsonl-v1"])
        self.assertNotIn("confirmed-cancellation", capabilities["a2a-0.3-jsonrpc"])
        remote = copy.deepcopy(self.raw)
        remote.pop("launch")
        remote.update(
            bridge="a2a-0.3-jsonrpc",
            remote={
                "endpoint": "https://endpoint.example/rpc",
                "card_url": "https://endpoint.example/card",
                "skill_id": "work",
                "artifact_prefix": "https://endpoint.example/artifacts/",
            },
        )
        remote["application_contract"]["requires"].pop("worker-jsonl")
        remote["application_contract"]["requires"]["a2a-jsonrpc"] = ">=0.3,<0.4"
        baseline = (
            await Revisions(self.db, "one").create(
                "remote-app", compile_profile(remote), actor="operator"
            )
        )["revision"]["id"]
        remote_required = copy.deepcopy(remote["application_contract"])
        remote_required["requires"]["confirmed-cancellation"] = ">=1,<2"
        response = await self.client.post(
            "/api/os/runtime/applications/compatibility",
            headers=self.headers,
            json={"revision_ref": baseline, "contract": remote_required},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["compatible"])
        self.assertEqual(
            response.json()["issues"],
            [
                {
                    "code": "APPLICATION_INTERFACE_UNSUPPORTED",
                    "interface": "confirmed-cancellation",
                    "required": ">=1,<2",
                    "available": None,
                }
            ],
        )
        remote["application_contract"] = remote_required
        # Neither absent support nor an unverified self-declaration can grant it.
        for declared in (False, True, None):
            remote["capabilities"] = {"cancel": declared}
            with self.assertRaisesRegex(
                RuntimeErrorCode, "APPLICATION_INTERFACE_UNSUPPORTED"
            ):
                compile_profile(remote)
        self.assertEqual((await self.console.list())["items"], [])

    async def test_version_and_requirement_errors_are_bounded(self):
        await self.setup_contract()
        for field, value in [
            ("requires", {"runtime-api": "nonsense"}),
            ("version", "banana"),
            ("deployment", "embedded"),
            ("input", {}),
        ]:
            candidate = copy.deepcopy(self.contract)
            candidate[field] = value
            with self.assertRaisesRegex(
                RuntimeErrorCode, "INVALID_APPLICATION_CONTRACT"
            ):
                manifest(candidate)
        revised = copy.deepcopy(self.raw)
        revised["application_contract"]["version"] = "1.0.1"
        self.assertTrue(compatibility(revised)["compatible"])
        self.assertNotEqual(compile_profile(revised), compile_profile(self.raw))
