"""Console activation binds immutable execution and input contracts."""

import copy
import json
import unittest

import test_runtime_task_console as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.execution_revisions import (
    compile_profile,
    freeze_console,
    load_profile,
)
from app.runtime.revisions import Revisions
from app.services.task_console import TaskConsole
from work_auth_fixtures import policy_fixture


class ConsoleRevisionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = fixtures.TaskConsoleTests.asyncTearDown

    async def publish(self, name="Original", governed=False):
        raw = copy.deepcopy(self.profile)
        raw["launch"]["image"] = "sha256:" + "1" * 64
        raw["console"]["name"] = name
        if governed:
            raw["governance"] = {"mode": "e2ag"}
        freeze_console(raw)
        result = await Revisions(self.db, "one").create(
            name, compile_profile(raw), actor="test-operator"
        )
        return result["revision"]["id"]

    def activate(self, ref, enabled=True, profile_id="review"):
        (self.profiles / "review.json").write_text(
            json.dumps({"id": profile_id, "revision_ref": ref, "enabled": enabled})
        )

    async def submit(self, key="first"):
        return await self.console.submit("review", "first-item", "检查证据", key)

    async def test_frozen_catalog_survives_source_deletion_and_new_activation(self):
        old = await self.publish()
        new = await self.publish("Updated")
        self.activate(old)
        self.input_catalog.unlink()
        response = await self.client.get(
            "/api/os/runtime/profiles", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["revision_ref"], old)
        self.assertNotIn("test-command", response.text)
        self.assertEqual(len(await self.console.samples()), 2)
        first = await self.submit()
        self.assertEqual(first["revision_ref"], old)
        self.assertEqual(first["id"], (await self.submit())["id"])
        self.activate(new)
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            await self.submit()
        second = await self.submit("new-request")
        self.assertEqual(second["revision_ref"], new)
        self.assertEqual((await self.console.detail(first["id"]))["revision_ref"], old)
        persisted = await self.console.store.get(first["id"])
        self.assertTrue(persisted["input"]["revision_digest"])
        self.assertEqual(persisted["state"], "queued")
        self.assertEqual(persisted["attempts"], [])

    async def test_activation_and_scope_do_not_grant_execution_authority(self):
        ref = await self.publish(governed=True)
        self.activate(ref)
        _, name, spec, _, _ = await load_profile(self.db, "one", ref)
        policy, _ = policy_fixture(self.root, name, spec)
        self.console.policy = policy
        first = await self.submit()
        self.assertEqual(first["revision_ref"], ref)
        config = json.loads(policy.path.read_text())
        config["profiles"] = {}
        policy.path.write_text(json.dumps(config))
        with self.assertRaises(RuntimeErrorCode):
            await self.submit("revoked")
        wrong = TaskConsole(self.db, "foreign", self.profiles, self.root / "other")
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await wrong.catalog()
        self.activate(ref, profile_id="another-definition")
        with self.assertRaisesRegex(
            RuntimeErrorCode, "REVISION_EXECUTION_BINDING_INVALID"
        ):
            await self.console.catalog()
        self.activate(ref, enabled=False)
        self.assertEqual(await self.console.catalog(), [])
        with self.assertRaisesRegex(RuntimeErrorCode, "PROFILE_NOT_AVAILABLE"):
            await self.submit("disabled")

    async def test_schedule_detects_revision_change_even_with_same_launch(self):
        old = await self.publish(governed=True)
        new = await self.publish("Updated", governed=True)
        self.activate(old)
        _, name, spec, _, _ = await load_profile(self.db, "one", old)
        policy, grant = policy_fixture(self.root, name, spec)
        self.console.policy = policy
        prepared = await self.console.prepare_submission("review", "first-item", "goal")
        binding = self.console.schedule_binding(prepared, grant)
        self.activate(new)
        with self.assertRaisesRegex(RuntimeErrorCode, "SCHEDULE_BINDING_CHANGED"):
            await self.console.submit(
                "review",
                "first-item",
                "goal",
                "scheduled",
                expected_schedule_binding=binding,
            )


if __name__ == "__main__":
    unittest.main()
