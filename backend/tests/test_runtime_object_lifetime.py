"""Retention, tombstones and derivation invalidation across real access paths."""

import asyncio
import os
import time
import unittest
from unittest.mock import patch

from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.object_lifetime import ObjectLifetime
from app.runtime.objects import reference
from app.runtime.recovery import backup_sqlite, restore_sqlite
from app.services.object_access import ObjectReader
import test_runtime_objects as objects
import test_runtime_object_access as access
import test_runtime_child_channel as children
from app.runtime.worker import Worker
from test_runtime_worker import FakeAdapter


class LifetimeScenarios:
    postgres = False
    asyncSetUp = objects.ObjectScenarios.asyncSetUp
    asyncTearDown = objects.ObjectScenarios.asyncTearDown

    async def test_retention_cas_expiration_and_replay_never_revive(self):
        ref = (await self.catalog.publish(self.provider, [b"one"], "one", "operator"))[
            "ref"
        ]
        lifetime = ObjectLifetime(self.db, self.scope)
        now = time.time()
        arguments = dict(
            expected_revision=0,
            key="retention",
            actor="operator",
            retain_until=now + 100,
            expires_at=now + 200,
        )
        first = await lifetime.change(ref, **arguments)
        with self.assertRaisesRegex(RuntimeErrorCode, "OBJECT_RETENTION_ACTIVE"):
            await lifetime.change(
                ref, expected_revision=1, key="delete", actor="operator", delete=True
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "OBJECT_RETENTION_SHORTENED"):
            await lifetime.change(
                ref,
                expected_revision=1,
                key="shorten",
                actor="operator",
                retain_until=now + 50,
            )
        with patch.object(self.db, "now", return_value=now + 300):
            self.assertEqual((await lifetime.show(ref))["state"], "expired")
            self.assertEqual(await lifetime.change(ref, **arguments), first)
            with self.assertRaisesRegex(RuntimeErrorCode, "OBJECT_ALREADY_UNAVAILABLE"):
                await lifetime.change(
                    ref, expected_revision=1, key="revive", actor="operator"
                )

    async def test_deletion_derivation_and_shared_bytes_are_independent(self):
        refs = [
            (await self.catalog.publish(self.provider, [b"same"], key, "operator"))[
                "ref"
            ]
            for key in ["source", "derived", "other"]
        ]
        source, derived, other = refs
        lifetime = ObjectLifetime(self.db, self.scope)
        await self.catalog.link(derived, "derived_from", source, "operator")
        with self.assertRaisesRegex(RuntimeErrorCode, "OBJECT_DERIVATION_CYCLE"):
            await self.catalog.link(source, "derived_from", derived, "operator")
        deleted = await lifetime.change(
            source, expected_revision=0, key="delete", actor="operator", delete=True
        )
        self.assertEqual(deleted["state"], "deleted")
        self.assertEqual((await lifetime.show(derived))["state"], "source_unavailable")
        self.assertEqual((await lifetime.show(derived))["cause"], source)
        self.assertEqual((await lifetime.show(other))["state"], "active")
        self.assertEqual(
            (await self.catalog.relations(derived))["items"][0]["target"], source
        )
        self.assertEqual(
            b"".join(
                self.provider.read((await self.catalog.get(other))["storage"]["key"])
            ),
            b"same",
        )
        new = (
            await self.catalog.publish(
                self.provider,
                [b"new"],
                "new",
                "operator",
                object_id=source["id"],
                expected_version=1,
            )
        )["ref"]
        self.assertEqual((await lifetime.show(new))["state"], "active")
        self.assertEqual((await lifetime.show(derived))["state"], "source_unavailable")

    async def test_concurrent_delete_single_revision_and_restart(self):
        ref = (
            await self.catalog.publish(self.provider, [b"content"], "one", "operator")
        )["ref"]
        lifetime = ObjectLifetime(self.db, self.scope)
        results = await asyncio.gather(
            *[
                lifetime.change(
                    ref, expected_revision=0, key=key, actor="operator", delete=True
                )
                for key in ["a", "b"]
            ],
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(
            [r.code for r in results if isinstance(r, RuntimeErrorCode)],
            ["VERSION_CONFLICT"],
        )
        await self.db.close()
        self.db = Database(self.url)
        self.assertEqual(
            (await ObjectLifetime(self.db, self.scope).show(ref))["state"], "deleted"
        )

    async def test_backup_keeps_tombstone_and_source_links(self):
        if self.postgres:
            self.skipTest("SQLite backup contract")
        source = (
            await self.catalog.publish(self.provider, [b"source"], "source", "operator")
        )["ref"]
        child = (
            await self.catalog.publish(self.provider, [b"child"], "child", "operator")
        )["ref"]
        await self.catalog.link(child, "derived_from", source, "operator")
        await ObjectLifetime(self.db, self.scope).change(
            source, expected_revision=0, key="delete", actor="operator", delete=True
        )
        bundle = self.root / "backup"
        await asyncio.to_thread(backup_sqlite, self.url, self.provider.root, bundle)
        destination = self.root / "restored"
        await restore_sqlite(bundle, destination)
        paths = list(destination.rglob("*.sqlite")) + list(destination.rglob("*.db"))
        self.assertEqual(len(paths), 1)
        db = Database("sqlite+aiosqlite:///" + str(paths[0]))
        try:
            self.assertEqual(
                (await ObjectLifetime(db, self.scope).show(child))["state"],
                "source_unavailable",
            )
        finally:
            await db.close()


class SQLiteLifetime(LifetimeScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "PostgreSQL not configured"
)
class PostgresLifetime(LifetimeScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class LifetimeAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = access.ObjectAccessTests.asyncSetUp
    asyncTearDown = access.ObjectAccessTests.asyncTearDown
    save = access.ObjectAccessTests.save
    headers_for = access.ObjectAccessTests.headers_for
    submit_as = access.ObjectAccessTests.submit_as
    get = access.ObjectAccessTests.get
    finish = access.ObjectAccessTests.finish
    path = access.ObjectAccessTests.path
    publish = access.ObjectAccessTests.publish
    set_policy = access.ObjectAccessTests.set_policy
    bound = access.ObjectAccessTests.bound

    async def test_delete_blocks_all_original_and_new_download_paths(self):
        run = await self.submit_as("a")
        await self.finish()
        result = (await self.get("a", "runs/" + run["id"])).json()
        ref = result["artifacts"][0]["object_ref"]
        path = self.path(ref)
        body = {
            "expected_revision": 0,
            "idempotency_key": "delete",
            "reason": "operator-test",
        }
        denied = await self.client.post(
            path + "/delete", headers=self.headers_for("a"), json=body
        )
        self.assertEqual(denied.status_code, 403)
        response = await self.client.post(
            path + "/delete", headers=self.headers, json=body
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            (
                await self.client.post(
                    path + "/delete", headers=self.headers, json=body
                )
            ).json(),
            response.json(),
        )
        for suffix in ["", "/download"]:
            self.assertEqual(
                (
                    await self.client.get(path + suffix, headers=self.headers_for("a"))
                ).status_code,
                404,
            )
        for suffix in ["/download", "/preview", "/download?format=markdown"]:
            self.assertEqual(
                (
                    await self.get(
                        "a", f"runs/{run['id']}/artifacts/{ref['id']}" + suffix
                    )
                ).status_code,
                404,
            )
        tombstone = (
            await self.client.get(path + "/lifecycle", headers=self.headers)
        ).json()
        self.assertEqual(tombstone["binding"]["run_id"], run["id"])
        self.assertEqual(tombstone["state"], "deleted")
        detail = (await self.get("a", "runs/" + run["id"])).json()
        self.assertEqual(detail["id"], run["id"])
        self.assertNotIn(ref["id"], [v["id"] for v in detail["artifacts"]])

    async def test_expiry_and_deletion_apply_to_already_open_reader(self):
        ref = await self.publish(data=b"firstsecond")
        await self.set_policy(ref, ["a"])
        view = await self.bound()
        reader = ObjectReader(view)
        value = await reader.get(ref)
        with patch.object(
            view.provider, "read", side_effect=lambda _: iter([b"first", b"second"])
        ):
            stream = reader.stream(value)
            self.assertEqual(await anext(stream), b"first")
            await ObjectLifetime(self.db, "one").change(
                ref, expected_revision=0, key="delete", actor="operator", delete=True
            )
            with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
                await anext(stream)
        other = await self.publish("other")
        await self.set_policy(other, ["a"], key="other-grant")
        now = time.time()
        response = await self.client.put(
            self.path(other) + "/retention",
            headers=self.headers,
            json={
                "expected_revision": 0,
                "idempotency_key": "expire",
                "retain_until": 0,
                "expires_at": now + 10,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        with patch.object(self.db, "now", return_value=now + 20):
            self.assertEqual(
                (
                    await self.client.get(
                        self.path(other) + "/download", headers=self.headers_for("a")
                    )
                ).status_code,
                404,
            )


class ChildLifetimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = children.ChildChannelTests.asyncSetUp
    asyncTearDown = children.ChildChannelTests.asyncTearDown
    submit = children.ChildChannelTests.submit

    async def test_governed_child_channel_cannot_return_deleted_result(self):
        child = await self.submit()
        f = self.f
        worker = Worker(
            f.store,
            FakeAdapter(),
            "test-profile",
            f.spec,
            f.provider,
            authorization_verifier=f.verifier,
        )
        self.assertEqual((await worker.once())["id"], child["id"])
        first = await self.client.child(child["id"])
        self.assertIn("result", first)
        artifact = next(a for a in first["artifacts"] if a["kind"] == "result")
        await ObjectLifetime(f.store.db, f.store.scope).change(
            reference("artifact", artifact["id"]),
            expected_revision=0,
            key="delete",
            actor="operator",
            delete=True,
        )
        second = await self.client.child(child["id"])
        self.assertNotIn("result", second)
        self.assertNotIn(artifact["id"], [a["id"] for a in second["artifacts"]])
