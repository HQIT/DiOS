"""Object-specific revocation at API, original artifact and storage boundaries."""

import asyncio
import base64
import os
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select
from starlette.requests import Request

from app.api.os import runtime_tasks
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.object_access import ObjectPolicies, subject
from app.runtime.object_access_schema import decisions
from app.runtime.objects import Objects
from app.runtime.principals import FilePrincipals
from app.runtime.recovery import backup_sqlite, restore_sqlite
from app.services.object_access import ObjectReader
import test_runtime_objects as fixtures


class ObjectAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ObjectApiTests.asyncSetUp
    asyncTearDown = fixtures.ObjectApiTests.asyncTearDown
    save = fixtures.ObjectApiTests.save
    headers_for = fixtures.ObjectApiTests.headers_for
    submit_as = fixtures.ObjectApiTests.submit_as
    get = fixtures.ObjectApiTests.get
    finish = fixtures.ObjectApiTests.finish

    def path(self, ref):
        return f"/api/os/runtime/objects/{ref['kind']}/{ref['id']}/versions/{ref['version']}"

    async def publish(self, key="file", data=b"content", **extra):
        response = await self.client.post(
            "/api/os/runtime/objects/files",
            headers=self.headers,
            json={
                "content_base64": base64.b64encode(data).decode(),
                "idempotency_key": key,
                **extra,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["ref"]

    async def set_policy(self, ref, ids, revision=0, key="grant", actions=None):
        return await self.client.put(
            self.path(ref) + "/policy",
            headers=self.headers,
            json={
                "grants": [
                    {
                        "principal_id": k,
                        "actions": actions or ["object.read", "object.download"],
                        "expires_at": time.time() + 300,
                    }
                    for k in ids
                ],
                "expected_revision": revision,
                "idempotency_key": key,
            },
        )

    async def bound(self, k="a"):
        provider = FilePrincipals(self.identities)
        request = Request(
            {
                "type": "http",
                "app": self.app,
                "headers": [(b"authorization", ("Bearer " + self.tokens[k]).encode())],
            }
        )
        return await runtime_tasks.service(
            request, provider.authenticate(self.tokens[k], "one")
        )

    async def test_same_content_distinct_permissions_exact_version_and_filtered_links(
        self,
    ):
        first = await self.publish("one")
        second = await self.publish("two")
        self.assertEqual(
            (await self.set_policy(first, ["a"], key="a")).status_code, 200
        )
        self.assertEqual(
            (await self.set_policy(second, ["b"], key="b")).status_code, 200
        )
        for k, own, other in [("a", first, second), ("b", second, first)]:
            response = await self.client.get(
                self.path(own) + "/download", headers=self.headers_for(k)
            )
            self.assertEqual(response.content, b"content")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(
                (
                    await self.client.get(
                        self.path(other) + "/download", headers=self.headers_for(k)
                    )
                ).status_code,
                404,
            )
        latest = await self.publish(
            "next", b"new", object_id=first["id"], expected_version=1
        )
        self.assertEqual(
            (
                await self.client.get(self.path(latest), headers=self.headers_for("a"))
            ).status_code,
            404,
        )
        self.assertEqual(
            (await self.set_policy(latest, ["b"], key="latest")).status_code, 200
        )
        path = f"/api/os/runtime/objects/file/{first['id']}/versions"
        page = (
            await self.client.get(path + "?limit=1", headers=self.headers_for("b"))
        ).json()
        self.assertEqual(page, {"items": [], "next_after": 1})
        page = (
            await self.client.get(
                path + "?after=1&limit=1", headers=self.headers_for("b")
            )
        ).json()
        self.assertEqual([v["ref"] for v in page["items"]], [latest])
        catalog = Objects(self.db, "one")
        await catalog.link(first, "derived_from", second, "operator")
        await catalog.link(
            first,
            "associated_with",
            {"namespace": "app", "key": "opaque", "version": "1"},
            "operator",
        )
        links = (
            await self.client.get(
                self.path(first) + "/relations", headers=self.headers_for("a")
            )
        ).json()["items"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["target"]["key"], "opaque")

    async def test_read_grant_does_not_allow_download_or_policy_change(self):
        ref = await self.publish()
        await self.set_policy(ref, ["a"], actions=["object.read"])
        self.assertEqual(
            (
                await self.client.get(self.path(ref), headers=self.headers_for("a"))
            ).status_code,
            200,
        )
        self.assertEqual(
            (
                await self.client.get(
                    self.path(ref) + "/download", headers=self.headers_for("a")
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            (
                await self.client.get(
                    self.path(ref) + "/policy", headers=self.headers_for("a")
                )
            ).status_code,
            403,
        )
        response = await self.client.put(
            self.path(ref) + "/policy",
            headers=self.headers_for("a"),
            json={
                "grants": [],
                "expected_revision": 1,
                "idempotency_key": "self-change",
            },
        )
        self.assertEqual(response.status_code, 403)

    async def test_revoke_cas_replay_restart_and_backup_preserve_denial(self):
        ref = await self.publish()
        policies = ObjectPolicies(self.db, "one")
        principal = subject(
            FilePrincipals(self.identities).authenticate(self.tokens["a"], "one")
        )
        grants = [
            {
                "principal": principal,
                "actions": ["object.read", "object.download"],
                "expires_at": time.time() + 600,
            }
        ]
        first = await policies.change(ref, grants, 0, "original", "operator")
        await policies.change(ref, [], 1, "revoke", "operator")
        self.assertEqual(
            await policies.change(ref, grants, 0, "original", "operator"), first
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_CONFLICT"):
            await policies.change(ref, grants, 1, "stale", "operator")
        response = await self.client.get(
            self.path(ref) + "/download",
            headers={**self.headers_for("a"), "If-None-Match": "any"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["cache-control"], "no-store")
        async with self.db.read("one") as c:
            rows = (await c.execute(select(decisions))).mappings().all()
        self.assertTrue(
            any(
                r["revision"] == 2 and r["decision"]["decision"] == "deny" for r in rows
            )
        )
        bundle = self.root / "backup"
        await asyncio.to_thread(
            backup_sqlite,
            "sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite"),
            self.console.provider.root,
            bundle,
        )
        destination = self.root / "restored"
        await restore_sqlite(bundle, destination)
        paths = list(destination.rglob("*.sqlite")) + list(destination.rglob("*.db"))
        self.assertEqual(len(paths), 1)
        restored = Database("sqlite+aiosqlite:///" + str(paths[0]))
        try:
            saved = ObjectPolicies(restored, "one")
            self.assertEqual((await saved.show(ref))["revision"], 2)
            with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
                await saved.check(ref, principal, "object.read")
        finally:
            await restored.close()

    async def test_original_artifact_paths_and_task_projection_obey_acl(self):
        run = await self.submit_as("a")
        await self.finish()
        detail = (await self.get("a", "runs/" + run["id"])).json()
        artifact = detail["artifacts"][0]
        ref = artifact["object_ref"]
        path = "runs/" + run["id"] + "/artifacts/" + artifact["id"]
        await self.set_policy(ref, ["a"])
        self.assertEqual((await self.get("a", path + "/download")).status_code, 200)
        await self.set_policy(ref, [], 1, "revoke")
        for suffix in ["/download", "/preview", "/download?format=markdown"]:
            self.assertEqual((await self.get("a", path + suffix)).status_code, 404)
        self.assertEqual(
            (
                await self.client.get(self.path(ref), headers=self.headers_for("a"))
            ).status_code,
            404,
        )
        detail = (await self.get("a", "runs/" + run["id"])).json()
        self.assertNotIn(artifact["id"], [v["id"] for v in detail["artifacts"]])
        # A grant on an artifact never bypasses the original task's access boundary.
        await self.set_policy(ref, ["b"], 2, "other")
        self.assertEqual(
            (
                await self.client.get(self.path(ref), headers=self.headers_for("b"))
            ).status_code,
            404,
        )

    async def test_revocation_during_blocking_provider_read_drops_pending_chunk(self):
        ref = await self.publish(data=b"firstsecond")
        await self.set_policy(ref, ["a"])
        view = await self.bound()
        reader = ObjectReader(view)
        value = await reader.get(ref)
        entered = threading.Event()
        release = threading.Event()

        def chunks():
            yield b"first"
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test barrier timeout")
            yield b"second"

        with patch.object(view.provider, "read", side_effect=lambda _: chunks()):
            stream = reader.stream(value)
            self.assertEqual(await anext(stream), b"first")
            pending = asyncio.create_task(anext(stream))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                await self.set_policy(ref, [], 1, "revoke")
                release.set()
                with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
                    await pending
            finally:
                release.set()
                await stream.aclose()

    async def test_actual_request_token_revocation_stops_stream_without_catalog_revision_change(
        self,
    ):
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
            self.directory["principals"]["a"]["tokens"][0]["enabled"] = False
            self.save()
            with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_UNAUTHENTICATED"):
                await anext(stream)

    async def test_subject_reassignment_and_expired_grants_fail_closed(self):
        ref = await self.publish()
        await self.set_policy(ref, ["a"])
        self.directory["principals"]["a"]["subject"] = "different-subject"
        self.save()
        self.assertEqual(
            (
                await self.client.get(self.path(ref), headers=self.headers_for("a"))
            ).status_code,
            404,
        )
        self.directory["principals"]["a"]["subject"] = "subject:a"
        self.save()
        policy = ObjectPolicies(self.db, "one")
        with patch.object(self.db, "now", return_value=time.time() + 3600):
            with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
                await policy.check(
                    ref,
                    FilePrincipals(self.identities).authenticate(
                        self.tokens["a"], "one"
                    ),
                    "object.read",
                )


class PolicyScenarios:
    postgres = False
    asyncSetUp = fixtures.ObjectScenarios.asyncSetUp
    asyncTearDown = fixtures.ObjectScenarios.asyncTearDown

    async def test_simultaneous_policy_updates_one_winner_and_durable_replay(self):
        value = await self.catalog.publish(
            self.provider, [b"content"], "file", "operator"
        )
        ref = value["ref"]
        policies = ObjectPolicies(self.db, self.scope)
        identity = {
            "issuer": "issuer",
            "audience": "dios-runtime",
            "scope": self.scope,
            "id": "a",
            "subject": "a",
            "actor": "service:a",
        }
        grants = [
            {
                "principal": identity,
                "actions": ["object.read"],
                "expires_at": time.time() + 600,
            }
        ]
        results = await asyncio.gather(
            *[
                policies.change(ref, g, 0, k, "operator")
                for g, k in [(grants, "grant"), ([], "deny")]
            ],
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(
            [r.code for r in results if isinstance(r, RuntimeErrorCode)],
            ["VERSION_CONFLICT"],
        )
        await policies.change(ref, [], 1, "revoke", "operator")
        await self.db.close()
        self.db = Database(self.url)
        policies = ObjectPolicies(self.db, self.scope)
        winner = next(r for r in results if isinstance(r, dict))
        key = "grant" if winner["grants"] else "deny"
        self.assertEqual(
            await policies.change(ref, winner["grants"], 0, key, "operator"), winner
        )
        self.assertEqual((await policies.show(ref))["revision"], 2)
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await policies.check(ref, identity, "object.read")


class SQLitePolicies(PolicyScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "PostgreSQL not configured"
)
class PostgresPolicies(PolicyScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True
