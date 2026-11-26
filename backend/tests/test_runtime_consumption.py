"""Concurrent admission, uncertain cost, evidence and idempotent reconciliation."""

import asyncio
import json
import os
import subprocess
import sys
import unittest
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import select

import test_runtime_capacity as fixtures
import test_runtime_task_console as console_fixtures
from app.api.os.runtime_tasks import service
from app.runtime.artifacts import LocalArtifacts
from app.runtime.consumption import Consumption
from app.runtime.consumption_schema import reservations
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.objects import Objects
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.store import RuntimeStore
from app.runtime.schema import attempts

NATIVE_OBSERVATIONS = []


class ConsumptionScenarios:
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown
    worker = fixtures.CapacityScenarios.worker
    submit = fixtures.CapacityScenarios.submit

    async def account(self, **kw):
        return await Consumption(self.db, self.scope).put(
            kw.pop("account_id", "money"),
            kw.pop("selector", {"kind": "scope"}),
            kw.pop("unit", "micro:USD"),
            kw.pop("maximum", 10),
            kw.pop("per_attempt", 3),
            kw.pop("revision", 0),
            key=kw.pop("key", str(uuid4())),
            actor="operator",
            enforcement=kw.pop("enforcement", "reservation_only"),
            **kw,
        )

    async def evidence(self):
        obj = await Objects(self.db, self.scope).publish(
            LocalArtifacts(self.root / "artifacts"),
            [b'{"basis":"synthetic provider statement"}'],
            str(uuid4()),
            "operator",
        )
        return {
            "ref": obj["ref"],
            "digest": obj["descriptor"]["digest"],
            "basis": "operator_reconciliation",
        }

    async def cancel_attempt(self, run, attempt, worker):
        current = await self.store.get(run["id"])
        await self.store.cancel(run["id"], current["version"])
        await self.store.confirm_cancel(
            run["id"], attempt["id"], *worker, attempt["epoch"], "fixture-exit"
        )

    async def status(self):
        return (await Consumption(self.db, self.scope).status())["items"][0]

    async def test_independent_connections_reserve_atomically_before_claim(self):
        await self.account()
        for _ in range(12):
            await self.submit()
        databases = [Database(self.url) for _ in range(12)]
        try:
            stores = [RuntimeStore(db, self.scope) for db in databases]
            workers = [await self.worker(store) for store in stores]
            results = await asyncio.gather(
                *(s.claim(*w) for s, w in zip(stores, workers))
            )
            self.assertEqual(sum(x is not None for x in results), 3)
            row = await self.status()
            self.assertEqual(
                (row["reserved"], row["settled"], row["remaining"]), (9, 0, 1)
            )
            queued = await self.submit()
            self.assertEqual(
                (await self.store.get(queued["id"]))["consumption_budget"][
                    "blocked_reasons"
                ],
                ["CONSUMPTION_BUDGET_EXHAUSTED"],
            )
        finally:
            for db in databases:
                await db.close()

    async def test_strict_cap_rejects_opaque_harness_and_does_not_starve_other_profiles(
        self,
    ):
        await self.account(
            enforcement="required",
            selector={"kind": "profiles", "profiles": ["opaque"]},
        )
        for _ in range(130):
            await self.submit(profile="opaque")
        free = await self.submit(profile="other")
        worker = await self.worker()
        self.assertIsNone(await self.store.claim(*worker, profile_id="opaque"))
        self.assertEqual((await self.store.claim(*worker))["run_id"], free["id"])
        self.assertEqual(
            (await self.status())["admission_error"], "PROVIDER_COST_CAP_UNSUPPORTED"
        )
        self.assertEqual((await self.status())["reserved"], 0)
        other = RuntimeStore(self.db, str(uuid4()))
        run = await other.submit("other", {"runtime_profile": "opaque"})
        self.assertEqual(
            (await other.claim(*await self.worker(other)))["run_id"], run["id"]
        )

    async def test_cancellation_keeps_reservation_until_single_evidenced_settlement(
        self,
    ):
        await self.account(maximum=3)
        run = await self.submit()
        worker = await self.worker()
        a = await self.store.claim(*worker)
        manager = Consumption(self.db, self.scope)
        proof = await self.evidence()
        with self.assertRaisesRegex(RuntimeErrorCode, "EXECUTION_UNRESOLVED"):
            await manager.settle(
                "money", a["id"], 0, proof, key="unresolved", actor="operator"
            )
        await self.cancel_attempt(run, a, worker)
        self.assertEqual((await self.status())["unreconciled_attempts"], 1)
        await self.submit()
        self.assertIsNone(await self.store.claim(*worker))
        result = await asyncio.gather(
            *(
                manager.settle(
                    "money", a["id"], 1, proof, key="settle", actor="operator"
                )
                for _ in range(3)
            )
        )
        self.assertEqual(result, [result[0]] * 3)
        self.assertEqual(
            ((await self.status())["reserved"], (await self.status())["settled"]),
            (0, 1),
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "ALREADY_SETTLED"):
            await manager.settle(
                "money", a["id"], 0, proof, key="second", actor="operator"
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            await manager.settle(
                "money", a["id"], 0, proof, key="settle", actor="operator"
            )

    async def test_overspend_is_recorded_and_blocks_future_admission(self):
        await self.account()
        run = await self.submit()
        w = await self.worker()
        a = await self.store.claim(*w)
        await self.cancel_attempt(run, a, w)
        result = await Consumption(self.db, self.scope).settle(
            "money", a["id"], 12, await self.evidence(), key="over", actor="operator"
        )
        self.assertTrue(result["over_reserved"])
        self.assertEqual((await self.status())["remaining"], -2)
        await self.submit()
        self.assertIsNone(await self.store.claim(*w))

    async def test_failed_unlaunched_retry_never_reuses_previous_reservation(self):
        await self.account(maximum=6)
        run = await self.submit()
        w = await self.worker()
        a = await self.store.claim(*w)
        await self.store.reject_unlaunched(
            (run["id"], a["id"], *w, a["epoch"]), "SECRET_PROVIDER_UNAVAILABLE"
        )
        self.assertEqual((await self.status())["reserved"], 3)
        # A separate logical run consumes a separate Attempt reservation.
        await self.submit()
        b = await self.store.claim(*w)
        self.assertIsNotNone(b)
        self.assertEqual((await self.status())["reserved"], 6)
        self.assertIsNone(await self.store.claim(*w))

    async def test_mutation_cannot_relabel_or_erase_account_history(self):
        first = await self.account(key="create")
        self.assertEqual(first, await self.account(key="create"))
        await self.submit()
        await self.store.claim(*await self.worker())
        with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_CONFLICT"):
            await self.account()
        for kw in (
            {"unit": "tokens"},
            {"selector": {"kind": "profiles", "profiles": ["other"]}},
        ):
            with self.assertRaisesRegex(RuntimeErrorCode, "IDENTITY_IMMUTABLE"):
                await self.account(revision=1, **kw)
        await self.account(revision=1, maximum=1)
        self.assertEqual((await self.status())["reserved"], 3)
        self.assertEqual((await self.status())["remaining"], -2)

    async def test_settlement_requires_available_scoped_evidence(self):
        await self.account()
        run = await self.submit()
        w = await self.worker()
        a = await self.store.claim(*w)
        await self.cancel_attempt(run, a, w)
        proof = await self.evidence()
        manager = Consumption(self.db, self.scope)
        with self.assertRaisesRegex(RuntimeErrorCode, "DIGEST_MISMATCH"):
            await manager.settle(
                "money",
                a["id"],
                1,
                dict(proof, digest="sha256:" + "0" * 64),
                key="bad",
                actor="operator",
            )
        from app.runtime.object_lifetime import ObjectLifetime

        await ObjectLifetime(self.db, self.scope).change(
            proof["ref"],
            expected_revision=0,
            key="delete",
            actor="operator",
            reason="fixture",
            delete=True,
        )
        with self.assertRaises(RuntimeErrorCode):
            await manager.settle(
                "money", a["id"], 1, proof, key="deleted", actor="operator"
            )
        self.assertEqual((await self.status())["reserved"], 3)

    async def test_invalid_quantity_currency_and_default_strict_mode(self):
        for value in (-1, 1.5, True, 10**15 + 1):
            with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_CONSUMPTION"):
                await self.account(maximum=value)
        with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_CONSUMPTION_UNIT"):
            await self.account(unit="USD")
        record = await Consumption(self.db, self.scope).put(
            "strict",
            {"kind": "scope"},
            "tokens",
            100,
            10,
            0,
            key="default",
            actor="operator",
        )
        self.assertEqual(record["account"]["enforcement"], "required")

    async def test_lost_lease_restart_and_failed_claim_preserve_budget(self):
        await self.account(maximum=3)
        run = await self.submit()
        worker = await self.worker()
        with patch.object(
            self.store, "_event", side_effect=RuntimeError("event write failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "event write failed"):
                await self.store.claim(*worker)
        self.assertEqual((await self.status())["reserved"], 0)
        attempt = await self.store.claim(*worker)
        async with self.db.transaction(self.scope) as connection:
            await connection.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(lease_until=0)
            )
        await self.store.expire()
        await self.db.close()
        self.db = Database(self.url)
        self.store = RuntimeStore(self.db, self.scope)
        self.assertEqual((await self.status())["unreconciled_attempts"], 1)
        self.assertEqual(
            (await self.store.get(run["id"]))["state"], "reconciliation_required"
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "EXECUTION_UNRESOLVED"):
            await Consumption(self.db, self.scope).settle(
                "money",
                attempt["id"],
                0,
                await self.evidence(),
                key="not-dead",
                actor="operator",
            )
        await self.submit()
        self.assertIsNone(await self.store.claim(*await self.worker()))
        current = await self.store.get(run["id"])
        await self.store.reconcile_absent(
            run["id"], current["version"], "confirmed-no-external-action"
        )
        await Consumption(self.db, self.scope).settle(
            "money",
            attempt["id"],
            0,
            await self.evidence(),
            key="resolved",
            actor="operator",
        )
        self.assertEqual((await self.status())["reserved"], 0)

    async def test_overlapping_token_and_money_accounts_intersect(self):
        await self.account(maximum=10)
        await self.account(account_id="tokens", unit="tokens", maximum=4, per_attempt=2)
        for _ in range(3):
            await self.submit()
        worker = await self.worker()
        self.assertIsNotNone(await self.store.claim(*worker))
        self.assertIsNotNone(await self.store.claim(*worker))
        self.assertIsNone(await self.store.claim(*worker))
        rows = (await Consumption(self.db, self.scope).status())["items"]
        self.assertEqual(
            [(row["unit"], row["reserved"]) for row in rows],
            [("micro:USD", 6), ("tokens", 4)],
        )


class SQLiteConsumptionTests(ConsumptionScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_backup_preserves_unsettled_and_settled_identity(self):
        await self.account()
        run = await self.submit()
        w = await self.worker()
        a = await self.store.claim(*w)
        await self.cancel_attempt(run, a, w)
        await Consumption(self.db, self.scope).settle(
            "money", a["id"], 1, await self.evidence(), key="posted", actor="operator"
        )
        await self.submit()
        await self.store.claim(*w)
        destination = self.root / "backup"
        await asyncio.to_thread(
            backup_sqlite, self.url, self.root / "artifacts", destination
        )
        report = await asyncio.to_thread(verify_backup, destination)
        self.assertIn("runtime_consumption_schema", report["schemas"])
        from app.runtime.recovery import restore_sqlite

        target = self.root / "restored"
        await restore_sqlite(destination, target)
        restored = Database("sqlite+aiosqlite:///" + str(target / "state.sqlite"))
        try:
            after = (await Consumption(restored, self.scope).status())["items"]
            self.assertEqual(
                after, (await Consumption(self.db, self.scope).status())["items"]
            )
            async with restored.read(self.scope) as connection:
                rows = (
                    (
                        await connection.execute(
                            select(reservations.c.attempt_id).where(
                                reservations.c.scope == self.scope
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                self.assertIn(a["id"], rows)
        finally:
            await restored.close()


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "PostgreSQL opt-in required"
)
class PostgresConsumptionTests(ConsumptionScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class ConsumptionAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = console_fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = console_fixtures.TaskConsoleTests.asyncTearDown

    async def test_operator_api_and_cli_use_same_contract(self):
        body = dict(
            account_id="tokens",
            selector={"kind": "scope"},
            unit="tokens",
            maximum=100,
            per_attempt=10,
            expected_revision=0,
            key="create",
            actor="operator",
        )
        response = await self.client.put(
            "/api/os/runtime/consumption/accounts", json=body, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        path = self.root / "account.json"
        path.write_text(json.dumps(body))
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "app.runtime.cli",
                "--database-url",
                "sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite"),
                "--scope",
                "one",
                "consumption",
                "set",
                "--file",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), response.json())
        status = await self.client.get(
            "/api/os/runtime/consumption", headers=self.headers
        )
        self.assertEqual(
            status.json()["items"][0]["admission_error"],
            "PROVIDER_COST_CAP_UNSUPPORTED",
        )
        run = await self.console.submit(
            "review", "first-item", "budget fixture", "blocked"
        )
        detail = await self.client.get(
            "/api/os/runtime/runs/" + run["id"], headers=self.headers
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(
            detail.json()["consumption_budget"]["blocked_reasons"],
            ["PROVIDER_COST_CAP_UNSUPPORTED"],
        )
        malformed = await self.client.post(
            "/api/os/runtime/consumption/settlements",
            headers=self.headers,
            json={
                "account_id": "tokens",
                "attempt_id": "unused",
                "actual": 0,
                "key": "invalid",
                "actor": "operator",
                "evidence": {"ref": {}, "digest": "bad", "basis": []},
            },
        )
        self.assertEqual((malformed.status_code, malformed.json()['detail']), (409, 'CONSUMPTION_EVIDENCE_REQUIRED'))
        self.assertEqual(
            (await self.client.get("/api/os/runtime/consumption")).status_code, 401
        )
        self.app.dependency_overrides[service] = lambda: self.console.for_principal(
            {"id": "application"}
        )
        try:
            for method, path, payload in [
                ("get", "/api/os/runtime/consumption", None),
                ("put", "/api/os/runtime/consumption/accounts", body),
            ]:
                response = await self.client.request(
                    method,
                    path,
                    headers=self.headers,
                    **({"json": payload} if payload else {}),
                )
                self.assertEqual(response.status_code, 403)
        finally:
            self.app.dependency_overrides.clear()


@unittest.skipUnless(os.environ.get("DIOS_TEST_DOCKER"), "Docker opt-in required")
class NativeConsumptionTests(unittest.IsolatedAsyncioTestCase):
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown
    account = ConsumptionScenarios.account
    evidence = ConsumptionScenarios.evidence

    async def test_native_e2ag_success_settlement_revocation_and_unsupported_cap(self):
        import docker
        from app.runtime.adapter import LaunchSpec
        from app.runtime.docker_adapter import DockerRuntimeAdapter
        from app.runtime.worker import Worker
        from app.runtime.work_authorizations import WorkAuthorizationVerifier
        from work_auth_fixtures import policy_fixture
        from test_runtime_worker import protocol

        client = docker.from_env()
        try:
            image = client.images.get("dios-backend:registry-acceptance-20261126").id
        finally:
            client.close()
        spec = LaunchSpec(
            image,
            (
                "python",
                "-c",
                "import sys;sys.stdout.buffer.write(" + repr(protocol()) + ")",
            ),
        )
        policy, _ = policy_fixture(self.root, "native-consumption", spec)
        await self.account(maximum=6)
        provider = LocalArtifacts(self.root / "artifacts")
        adapter = DockerRuntimeAdapter(self.scope)
        worker = Worker(
            self.store,
            adapter,
            "native-consumption",
            spec,
            provider,
            poll_interval=0.03,
            authorization_verifier=WorkAuthorizationVerifier(self.store, policy),
        )

        async def submit(key):
            return await self.store.submit(
                key,
                {
                    "runtime_profile": "native-consumption",
                    "profile_digest": spec.fingerprint(),
                    "secret_refs": [],
                    "payload": {},
                },
                authorization=policy.admission(
                    "native-consumption", spec.fingerprint(), []
                ),
            )

        try:
            await submit("success")
            result = await worker.once()
            self.assertEqual(result["state"], "succeeded")
            a = result["attempts"][0]
            self.assertEqual(result["consumption_budget"]["items"][0]["actual"], None)
            settled = await Consumption(self.db, self.scope).settle(
                "money",
                a["id"],
                2,
                await self.evidence(),
                key="native-settlement",
                actor="operator",
            )
            denied = await submit("revoked")
            await self.store.revoke(denied["id"], denied["version"])
            self.assertEqual((await worker.once())["state"], "idle")
            held = await submit("strict")
            await self.account(account_id="hard", enforcement="required")
            self.assertEqual((await worker.once())["state"], "idle")
            current = await self.store.get(held["id"])
            self.assertEqual(current["attempts"], [])
            self.assertEqual(
                current["consumption_budget"]["blocked_reasons"],
                ["PROVIDER_COST_CAP_UNSUPPORTED"],
            )
            NATIVE_OBSERVATIONS.append(
                {
                    "image": image,
                    "run": result["id"],
                    "attempt": a["id"],
                    "session": a["session_id"],
                    "state": result["state"],
                    "process": a["remote"]["runtime"],
                    "settled": settled["settlement"]["actual"],
                    "revoked_run": denied["id"],
                    "strict_run": held["id"],
                    "strict_attempts": 0,
                }
            )
        finally:
            adapter.client.close()
