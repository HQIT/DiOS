import asyncio
import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

import jsonpatch
import test_runtime_task_console as fixtures
from app.api.os import configuration_studio as api
from app.runtime.store import encoded
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker, read_profile
from app.services import configuration_studio as studio
from work_auth_fixtures import policy_fixture

ROOT = Path(__file__).resolve().parents[2]


class StudioTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = fixtures.TaskConsoleTests.asyncTearDown

    async def route(self, path, body):
        # Include once per test; the existing fixture already has runtime task routes.
        if not getattr(self, "studio_routes", False):
            self.app.include_router(api.router, prefix="/api/os")
            self.studio_routes = True
        return await self.client.post(
            "/api/os/runtime/studio/" + path, headers=self.headers, json=body
        )

    async def assistant(self):
        raw = copy.deepcopy(self.profile)
        raw["id"] = "studio-assistant"
        raw["governance"] = {"mode": "e2ag"}
        catalog = self.root / "studio-inputs.json"
        catalog.write_bytes((ROOT / "examples/runtime/studio-inputs.json").read_bytes())
        raw["console"].update(input_kind=studio.KIND, input_catalog=str(catalog))
        (self.profiles / "studio.json").write_text(json.dumps(raw))
        _, spec = read_profile("test", value=raw)
        self.console.policy, _ = policy_fixture(self.root, raw["id"], spec)
        return spec

    async def test_schema_locations_repair_and_rfc_patch_do_not_change_source(self):
        before = (self.profiles / "review.json").read_bytes()
        base = json.dumps(self.profile)
        draft = copy.deepcopy(self.profile)
        draft["launch"]["cpu_count"] = 0
        response = await self.route(
            "compare", {"base": base, "draft": json.dumps(draft)}
        )
        value = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(value["valid"])
        self.assertEqual(value["errors"][0]["path"], "/launch/cpu_count")
        draft["launch"]["cpu_count"] = 2
        draft["launch"]["environment"]["中文/~"] = "值🙂"
        response = await self.route(
            "compare", {"base": base, "draft": json.dumps(draft)}
        )
        value = response.json()
        self.assertTrue(value["valid"])
        self.assertEqual(jsonpatch.apply_patch(self.profile, value["patch"]), draft)
        self.assertIn(
            "/launch/environment/中文~1~0", [p["path"] for p in value["patch"]]
        )
        self.assertEqual((self.profiles / "review.json").read_bytes(), before)
        self.assertEqual((await self.console.list())["items"], [])

    async def test_invalid_json_bounds_and_credentials_never_enter_diff(self):
        base = json.dumps(self.profile)
        for text in (
            '{"id":',
            '{"id":1,"id":2}',
            '{"x":NaN}',
            '{"x":1e9999}',
            '{"x":"\\ud800"}',
            '{"x":' + "[" * 40 + "0" + "]" * 40 + "}",
        ):
            result = studio.compare(base, text)
            self.assertFalse(result["valid"])
            self.assertEqual(result["patch"], [])
        self.assertEqual(studio.parse('{\n"id": }')[1][0]["line"], 2)
        secret = copy.deepcopy(self.profile)
        secret["launch"]["environment"]["API_KEY"] = "private-test-value"
        result = studio.compare(base, json.dumps(secret))
        self.assertFalse(result["valid"])
        self.assertNotIn("private-test-value", json.dumps(result))
        self.assertFalse(studio.compare(base, "x" * 65537)["valid"])
        unknown = dict(self.profile, recovrey={})
        self.assertFalse(studio.compare(base, json.dumps(unknown))["valid"])

    async def test_operator_gate_and_profile_copy_keep_execution_private(self):
        await self.route("compare", {"base": "{}", "draft": "{}"})
        path = "/api/os/runtime/studio/profiles/review"
        denied = await self.client.get(path)
        self.assertEqual(denied.status_code, 401)
        response = await self.client.get(path, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        expected = copy.deepcopy(self.profile)
        from app.runtime.execution_revisions import freeze_console

        freeze_console(expected)
        self.assertEqual(json.loads(response.json()["text"]), expected)
        extra = await self.route(
            "compare", {"base": "{}", "draft": "{}", "publish": True}
        )
        self.assertEqual(extra.status_code, 422)

    async def test_governed_suggestion_result_and_stale_review(self):
        spec = await self.assistant()
        base = json.dumps(self.profile)
        body = {
            "text": base,
            "profile_id": "studio-assistant",
            "instruction": "内存改为 512 MiB",
            "idempotency_key": "suggest-one",
        }
        first = await self.route("suggestions", body)
        self.assertEqual(first.status_code, 202, first.text)
        repeated = await self.route("suggestions", body)
        self.assertEqual(first.json()["id"], repeated.json()["id"])
        run_id = first.json()["id"]
        raw = await self.console.store.get(run_id)
        module_spec = importlib.util.spec_from_file_location(
            "config_agent", ROOT / "capabilities/configuration-assistant/agent.py"
        )
        agent = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(agent)
        prompt, decoder = agent.prepare(raw["input"]["payload"])
        self.assertIn("内存改为 512 MiB", prompt)
        draft = copy.deepcopy(self.profile)
        draft["launch"]["memory_bytes"] = 536870912
        result = decoder({"summary": "提高内存上限", "configuration": draft})
        data = b"".join(
            encoded(m) + b"\n"
            for m in [
                {"type": "session", "session_id": "proposal-session"},
                {"type": "result", "session_id": "proposal-session", "result": result},
            ]
        )
        adapter = fixtures.FakeAdapter(data)
        worker = Worker(
            self.console.store,
            adapter,
            "studio-assistant",
            spec,
            self.console.provider,
            authorization_verifier=WorkAuthorizationVerifier(
                self.console.store, self.console.policy
            ),
        )
        finished = await worker.once()
        self.assertEqual(finished["state"], "succeeded", finished)
        preview = await self.route(f"suggestions/{run_id}/review", {"text": base})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(json.loads(preview.json()["text"]), draft)
        self.assertEqual(adapter.started, 1)
        stale = await self.route(
            f"suggestions/{run_id}/review", {"text": json.dumps(draft)}
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"], "STUDIO_PROPOSAL_STALE")
        self.assertEqual(
            json.loads((self.profiles / "review.json").read_text()), self.profile
        )

    async def test_revoked_proposal_authority_prevents_enqueue(self):
        await self.assistant()
        policy = json.loads(self.console.policy.path.read_text())
        policy["profiles"] = {}
        self.console.policy.path.write_text(json.dumps(policy))
        response = await self.route(
            "suggestions",
            {
                "text": json.dumps(self.profile),
                "profile_id": "studio-assistant",
                "instruction": "调整资源",
                "idempotency_key": "denied",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.console.list())["items"], [])

    async def test_cli_and_api_share_validated_diff(self):
        base = json.dumps(self.profile)
        candidate = copy.deepcopy(self.profile)
        candidate["launch"]["pids_limit"] = 128
        first = self.root / "before.json"
        first.write_text(base)
        second = self.root / "after.json"
        second.write_text(json.dumps(candidate))
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                str(ROOT / "scripts/config_studio.py"),
                "--base",
                str(first),
                "--draft",
                str(second),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = await self.route(
            "compare", {"base": base, "draft": second.read_text()}
        )
        self.assertEqual(json.loads(proc.stdout), expected.json())
