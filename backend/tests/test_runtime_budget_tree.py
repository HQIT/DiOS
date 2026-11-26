"""Actual admission and E2AG tool dispatch across profiles and task generations."""

import asyncio
import json
import os
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import test_runtime_tool_invocations as tools_fixture
import test_runtime_worker as worker_fixture
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.capacity import Capacity, occupancy
from app.runtime.consumption import Consumption
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.execution_budgets import view as deadline_view
from app.runtime.store import RuntimeStore
from app.runtime.tool_budgets import view
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.work_policy import permission


class TreeScenarios:
    postgres = False
    asyncTearDown = tools_fixture.ToolInvocationTests.asyncTearDown

    async def asyncSetUp(self):
        original_policy = tools_fixture.policy_fixture
        original_claim = RuntimeStore.claim
        prepared = False

        def policy(*args):
            provider, descriptor = original_policy(*args)
            config = provider.configuration()
            row = config["profiles"]["test-profile"]
            for layer in row["permissions"].values():
                layer.append(permission(descriptor, "spawn"))
                layer.extend(
                    permission(dict(descriptor, profile_id="child-profile"), a)
                    for a in ("spawn", "execute", "accept_result")
                )
            config["profiles"]["child-profile"] = json.loads(json.dumps(row))
            provider.path.write_text(json.dumps(config))
            return provider, provider.admission(
                "test-profile", args[2].fingerprint(), []
            )

        async def claim(store, *args, **kw):
            nonlocal prepared
            if not prepared:
                prepared = True
                await Capacity(store).put(
                    "root",
                    {"kind": "profiles", "profiles": ["test-profile"]},
                    10,
                    0,
                    budget={"max_tool_calls": 2, "max_runtime_seconds": 300},
                )
                await Consumption(store.db, store.scope).put(
                    "money",
                    {"kind": "profiles", "profiles": ["test-profile"]},
                    "tokens",
                    9,
                    3,
                    0,
                    key="account",
                    actor="operator",
                    enforcement="reservation_only",
                )
            return await original_claim(store, *args, **kw)

        with (
            patch.object(tools_fixture, "policy_fixture", policy),
            patch.object(RuntimeStore, "claim", claim),
        ):
            if self.postgres:
                url = os.environ["DIOS_TEST_POSTGRES_URL"]
                with patch.object(worker_fixture, "Database", lambda _: Database(url)):
                    await tools_fixture.ToolInvocationTests.asyncSetUp(self)
                self.url = url
            else:
                await tools_fixture.ToolInvocationTests.asyncSetUp(self)
        config = self.policy.configuration()
        for layer in config["profiles"]["child-profile"]["permissions"].values():
            layer.append(self.permission)
        self.policy.path.write_text(json.dumps(config))

    async def child(self, parent=None, binding=None):
        parent = parent or self.run
        binding = binding or self.binding
        return await self.store.submit(
            str(uuid4()),
            dict(
                runtime_profile="child-profile",
                profile_digest=self.spec.fingerprint(),
                secret_refs=[],
            ),
            authorization=self.policy.admission(
                "child-profile", self.spec.fingerprint(), []
            ),
            parent_binding=dict(
                owner=list(binding),
                session_id="session-one",
                max_children=3,
                max_depth=3,
            ),
        )

    async def activate(self, run):
        worker = str(uuid4())
        await self.store.register_worker(worker, "instance", "host", os.getpid())
        attempt = await self.store.claim(worker, "instance", profile_id="child-profile")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt["run_id"], run["id"])
        binding = (run["id"], attempt["id"], worker, "instance", attempt["epoch"])
        context = WorkAuthorizationContext(
            self.scope,
            run["id"],
            attempt["id"],
            worker,
            "instance",
            attempt["epoch"],
            attempt["authorization_epoch"],
            None,
            run["input"]["work_authorization_ref"],
        )
        await self.verifier(context, "execute")
        await self.store.report(*binding, 1, "preparing")
        await self.store.report(*binding, 2, "creating_session")
        await self.verifier(replace(context, session_id="session-one"), "execute")
        await self.store.report(*binding, 3, "waiting_model", session_id="session-one")
        return binding

    async def invocation(self, run, binding, key):
        item = await self.gate.request(
            run["id"], binding[1], "session-one", "search", {"query": "evidence"}, key
        )
        return await self.gate.decide(
            run["id"], item["id"], item["version"], "approved", "fixture-approval"
        )

    async def test_children_share_ancestor_tools_with_independent_connections(self):
        child = await self.child()
        cb = await self.activate(child)
        grandchild = await self.child(child, cb)
        gb = await self.activate(grandchild)
        owners = [(self.run, self.binding), (child, cb), (grandchild, gb)]
        calls = [
            (r, await self.invocation(r, b, str(n))) for n, (r, b) in enumerate(owners)
        ]
        dbs = [Database(self.url) for _ in calls]
        try:
            gates = [
                ToolInvocations(
                    RuntimeStore(db, self.scope),
                    self.policy,
                    self.gate.catalog,
                    self.provider,
                    self.transport,
                    self.gate.approvers,
                )
                for db in dbs
            ]
            results = await asyncio.gather(
                *(
                    g.dispatch(r["id"], item["id"], item["version"])
                    for g, (r, item) in zip(gates, calls)
                ),
                return_exceptions=True,
            )
            self.assertEqual(sum(isinstance(x, dict) for x in results), 2)
            self.assertEqual(
                sum(
                    isinstance(x, RuntimeErrorCode)
                    and x.code == "TOOL_BUDGET_EXHAUSTED"
                    for x in results
                ),
                1,
            )
            self.assertEqual(len(self.transport.calls), 2)
            async with self.db.read(self.scope) as c:
                self.assertEqual(
                    (await view(c, self.scope, self.run["id"]))["committed"], 2
                )
                for r in [child, grandchild]:
                    value = await view(c, self.scope, r["id"])
                    self.assertTrue(value["inherited"])
                    self.assertTrue(value["exhausted"])
            # Replaying a settled tool does not consume any ancestor twice.
            n = next(n for n, x in enumerate(results) if isinstance(x, dict))
            r, item = calls[n]
            await gates[n].dispatch(r["id"], item["id"], item["version"])
            self.assertEqual(len(self.transport.calls), 2)
        finally:
            for db in dbs:
                await db.close()

    async def test_profile_change_cannot_escape_consumption_or_capacity(self):
        first = await self.child()
        await self.activate(first)
        second = await self.child()
        await self.activate(second)
        third = await self.child()
        await self.store.register_worker("blocked-" + self.scope, "instance", "host", os.getpid())
        self.assertIsNone(
            await self.store.claim("blocked-" + self.scope, "instance", profile_id="child-profile")
        )
        self.assertEqual(
            (await self.store.get(third["id"]))["consumption_budget"][
                "blocked_reasons"
            ],
            ["CONSUMPTION_BUDGET_EXHAUSTED"],
        )
        totals = (await Consumption(self.db, self.scope).status())["items"][0]
        self.assertEqual(totals["reserved"], 9)
        async with self.db.read(self.scope) as c:
            self.assertEqual(
                (
                    await occupancy(
                        c,
                        self.scope,
                        {"kind": "profiles", "profiles": ["test-profile"]},
                    )
                )[0],
                3,
            )
            # Parent and children matching the same selector are counted once.
            self.assertEqual(
                (
                    await occupancy(
                        c,
                        self.scope,
                        {
                            "kind": "profiles",
                            "profiles": ["test-profile", "child-profile"],
                        },
                    )
                )[0],
                3,
            )

    async def test_parent_deadline_persists_after_policy_disabled_and_restart(self):
        async with self.db.read(self.scope) as c:
            expected = await deadline_view(
                c, self.scope, self.run["id"], await self.db.now(c)
            )
        await Capacity(self.store).put(
            "root",
            {"kind": "profiles", "profiles": ["test-profile"]},
            10,
            1,
            enabled=False,
            budget={},
        )
        child = await self.child()
        await self.activate(child)
        await self.db.close()
        self.db = Database(self.url)
        self.store = RuntimeStore(self.db, self.scope)
        async with self.db.read(self.scope) as c:
            actual = await deadline_view(
                c, self.scope, child["id"], await self.db.now(c)
            )
            self.assertEqual(actual["deadline_at"], expected["deadline_at"])
            value = await view(c, self.scope, child["id"])
            self.assertIsNone(value["maximum"])
            self.assertEqual(value["remaining"], 2)
            self.assertTrue(value["inherited"])

    async def test_strict_cost_cap_is_inherited_by_queued_child(self):
        child = await self.child()
        await Consumption(self.db, self.scope).put(
            "money",
            {"kind": "profiles", "profiles": ["test-profile"]},
            "tokens",
            9,
            3,
            1,
            key="strict",
            actor="operator",
        )
        await self.store.register_worker("blocked-" + self.scope, "instance", "host", os.getpid())
        self.assertIsNone(
            await self.store.claim("blocked-" + self.scope, "instance", profile_id="child-profile")
        )
        self.assertEqual(
            (await self.store.get(child["id"]))["consumption_budget"][
                "blocked_reasons"
            ],
            ["PROVIDER_COST_CAP_UNSUPPORTED"],
        )

    async def test_unknown_tool_cost_survives_child_cancellation(self):
        child = await self.child()
        binding = await self.activate(child)
        item = await self.invocation(child, binding, "unknown")
        self.transport.failure = True
        await self.gate.dispatch(child["id"], item["id"], item["version"])
        current = await self.store.get(child["id"])
        await self.store.cancel(child["id"], current["version"])
        async with self.db.read(self.scope) as c:
            budget = await view(c, self.scope, self.run["id"])
            self.assertEqual(
                (budget["committed"], budget["remaining"], budget["unresolved"]),
                (1, 1, 1),
            )
        self.assertEqual(
            (await Consumption(self.db, self.scope).status())["items"][0]["reserved"], 6
        )

    async def test_parent_revocation_still_fences_child_dispatch(self):
        child = await self.child()
        binding = await self.activate(child)
        item = await self.invocation(child, binding, "revoked")
        parent = await self.store.get(self.run["id"])
        await self.store.revoke(parent["id"], parent["version"])
        with self.assertRaises(RuntimeErrorCode):
            await self.gate.dispatch(child["id"], item["id"], item["version"])
        self.assertEqual(self.transport.calls, [])
        async with self.db.read(self.scope) as c:
            self.assertEqual(
                (await view(c, self.scope, self.run["id"]))["committed"], 0
            )


class SQLiteTreeTests(TreeScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "isolated PostgreSQL required"
)
class PostgresTreeTests(TreeScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True
