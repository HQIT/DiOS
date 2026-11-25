"""Operator-only management, CAS and privacy-preserving queue explanations."""

import unittest

import test_runtime_principals as principals
import test_runtime_task_console as console
from app.runtime.capacity import Capacity
from app.runtime.capacity_schema import changes
from sqlalchemy import select


class CapacityApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = console.TaskConsoleTests.asyncSetUp
    asyncTearDown = console.TaskConsoleTests.asyncTearDown
    submit = console.TaskConsoleTests.submit

    async def put(self, **changes):
        value = {
            "id": "shared",
            "selector": {"kind": "scope"},
            "max_concurrent": 0,
            "expected_revision": 0,
            **changes,
        }
        return await self.client.put(
            "/api/os/runtime/capacity", json=value, headers=self.headers
        )

    async def test_operator_update_audit_and_conflict(self):
        self.assertEqual((await self.put()).status_code, 200)
        self.assertEqual((await self.put(max_concurrent=3)).status_code, 409)
        result = await self.put(max_concurrent=2, expected_revision=1)
        self.assertEqual(result.json()["revision"], 2)
        status = await self.client.get("/api/os/runtime/capacity", headers=self.headers)
        self.assertEqual(status.headers["cache-control"], "no-store")
        self.assertEqual(status.json()["items"][0]["available"], 2)
        async with self.db.read("one") as c:
            audit = (
                (await c.execute(select(changes).order_by(changes.c.revision)))
                .mappings()
                .all()
            )
        self.assertEqual(len(audit), 2)
        self.assertTrue(all(row["record"]["source"] == "operator_api" for row in audit))

    async def test_queue_explanation_updates_without_changing_run_version(self):
        run = await self.submit()
        await self.put()
        path = "/api/os/runtime/runs/" + run["id"]
        blocked = (await self.client.get(path, headers=self.headers)).json()
        self.assertTrue(blocked["capacity"]["blocked"])
        self.assertEqual(blocked["capacity"]["limits"][0]["max_concurrent"], 0)
        await self.put(enabled=False, expected_revision=1)
        released = (await self.client.get(path, headers=self.headers)).json()
        self.assertEqual(released["version"], blocked["version"])
        self.assertFalse(released["capacity"]["blocked"])

    async def test_invalid_or_unauthenticated_update_has_no_effect(self):
        for value in (True, -1, 1.5, 100001):
            self.assertEqual((await self.put(max_concurrent=value)).status_code, 422)
        self.assertEqual((await self.put(source="trusted_local")).status_code, 422)
        self.assertEqual(
            (await self.put(selector={"kind": "profiles", "profiles": []})).status_code,
            422,
        )
        response = await self.client.get("/api/os/runtime/capacity")
        self.assertEqual(response.status_code, 401)
        self.assertEqual((await Capacity(self.console.store).status())["items"], [])


class PrincipalCapacityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = principals.PrincipalTests.asyncSetUp
    asyncTearDown = console.TaskConsoleTests.asyncTearDown
    save = principals.PrincipalTests.save
    headers_for = principals.PrincipalTests.headers_for
    submit_as = principals.PrincipalTests.submit_as
    get = principals.PrincipalTests.get

    async def test_application_cannot_read_or_change_limits(self):
        await Capacity(self.console.store).put("private-pool", {"kind": "scope"}, 0, 0)
        self.assertEqual((await self.get("a", "capacity")).status_code, 403)
        response = await self.client.put(
            "/api/os/runtime/capacity",
            headers=self.headers_for("a"),
            json={
                "id": "private-pool",
                "selector": {"kind": "scope"},
                "max_concurrent": 100,
                "expected_revision": 1,
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            (await Capacity(self.console.store).status())["items"][0]["revision"], 1
        )

    async def test_own_queue_reason_does_not_expose_other_tasks_or_limits(self):
        capacity = Capacity(self.console.store)
        await capacity.put("secret-shared-account", {"kind": "scope"}, 0, 0)
        a, b = await self.submit_as("a"), await self.submit_as("b")
        result = (await self.get("a", "runs/" + a["id"])).json()
        self.assertEqual(
            result["capacity"],
            {"blocked": True, "reason": "SHARED_CAPACITY_EXHAUSTED", "limits": []},
        )
        self.assertNotIn("secret-shared-account", str(result))
        self.assertEqual((await self.get("a", "runs/" + b["id"])).status_code, 404)
        await capacity.put(
            "secret-shared-account", {"kind": "scope"}, 0, 1, enabled=False
        )
        await capacity.put(
            "subject-b", {"kind": "subject", "subject": "subject:b"}, 0, 0
        )
        self.assertFalse(
            (await self.get("a", "runs/" + a["id"])).json()["capacity"]["blocked"]
        )
        self.assertTrue(
            (await self.get("b", "runs/" + b["id"])).json()["capacity"]["blocked"]
        )


if __name__ == "__main__":
    unittest.main()
