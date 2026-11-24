"""Durable logout delivery fences Work without extending or losing identity history."""

import asyncio
import time
import unittest
from unittest.mock import patch, AsyncMock
from sqlalchemy import select, func
import test_runtime_oauth_sessions as sessions
import test_runtime_oauth_source as fixtures
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.logout_events import receive
from app.runtime.logout_schema import events
from app.runtime.store import RuntimeStore
from app.runtime.work_authorizations import view
from app.runtime.recovery import backup_sqlite, verify_backup


class LogoutTests(fixtures.SourceFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await sessions.MultiSourceTests.asyncSetUp(self)
        self.started = int(time.time()) - 60
        for name, ref in [("a", "session-a-v1"), ("peer", "peer")]:
            self.directory["oauth_sources"][ref].update(
                session_id=name, session_started_at=self.started
            )
            self.claims[self.tokens[name]].update(sid=name, auth_time=self.started)
        self.save()

    asyncTearDown = fixtures.OAuthPrincipalTests.asyncTearDown
    save = sessions.MultiSourceTests.save
    add_source = sessions.MultiSourceTests.add_source
    submit_as = sessions.MultiSourceTests.submit_as
    headers_for = sessions.MultiSourceTests.headers_for
    context = sessions.MultiSourceTests.context
    get = sessions.MultiSourceTests.get

    def event(self, **kwargs):
        now = int(time.time())
        return dict(
            issuer=self.config["issuer"],
            client_id="portal",
            subject="upstream-alice",
            session_id="a",
            event_id="logout-one",
            issued_at=now,
            expires_at=now + 60,
            **kwargs,
        )

    async def count(self):
        async with self.db.read("one") as c:
            return await c.scalar(select(func.count()).select_from(events))

    async def test_atomic_grant_revocation_peer_survives_and_future_work_fenced(self):
        run = await self.submit_as("a", "one")
        context = await self.context(run)
        await self.verifier(context, "execute")
        peer = await self.submit_as("peer", "peer")
        before = await view(self.console.store, run["id"])
        result = await receive(self.console.store, self.event(), "a" * 64)
        self.assertEqual(result["revoked_work_count"], 1)
        after = await view(self.console.store, run["id"])
        self.assertEqual(after["state"], "revoked")
        self.assertEqual(after["grant"]["state"], "revoked")
        self.assertEqual(after["expires_at"], before["expires_at"])
        self.assertEqual(after["source_event"]["id"], result["event_id"])
        self.assertEqual(
            (await view(self.console.store, peer["id"]))["state"], "active"
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.verifier(context, "execute")
        response = await self.client.post(
            "/api/os/runtime/runs",
            headers=self.headers_for("a"),
            json={
                "profile_id": "review",
                "sample_id": "first-item",
                "goal": "review",
                "idempotency_key": "after",
            },
        )
        self.assertEqual(response.status_code, 403)
        # Receipt replay reads history; it never recreates Work.
        replay = await self.submit_as("peer", "one")
        self.assertEqual(replay["id"], run["id"])
        self.assertEqual(replay["authorization"]["state"], "revoked")

    async def test_concurrent_duplicate_and_conflicting_id(self):
        await self.submit_as("a")
        results = await asyncio.gather(
            *(receive(self.console.store, self.event(), "a" * 64) for _ in range(4))
        )
        self.assertEqual(sum(not x["duplicate"] for x in results), 1)
        self.assertEqual(await self.count(), 1)
        changed = self.event()
        changed["session_id"] = "peer"
        with self.assertRaisesRegex(RuntimeErrorCode, "LOGOUT_EVENT_CONFLICT"):
            await receive(self.console.store, changed, "b" * 64)
        self.assertEqual(await self.count(), 1)

    async def test_concurrent_admission_cannot_escape_committed_logout(self):
        async def submit():
            return await self.client.post(
                "/api/os/runtime/runs",
                headers=self.headers_for("a"),
                json={
                    "profile_id": "review",
                    "sample_id": "first-item",
                    "goal": "review",
                    "idempotency_key": "race",
                },
            )

        result, _ = await asyncio.gather(
            submit(), receive(self.console.store, self.event(), "a" * 64)
        )
        self.assertIn(result.status_code, (202, 403))
        if result.status_code == 202:
            self.assertEqual(
                (await view(self.console.store, result.json()["id"]))["state"],
                "revoked",
            )

    async def test_wrong_issuer_client_subject_or_session_does_not_revoke(self):
        run = await self.submit_as("a")
        for key in ("issuer", "client_id", "subject", "session_id"):
            event = self.event()
            event[key] = "other"
            event["event_id"] = key
            self.assertEqual(
                (await receive(self.console.store, event, "a" * 64))[
                    "revoked_work_count"
                ],
                0,
            )
        self.assertEqual((await view(self.console.store, run["id"]))["state"], "active")

    async def test_subject_event_fences_old_and_unknown_but_preserves_new_authentication(
        self,
    ):
        a = await self.submit_as("a")
        started = int(time.time()) - 10
        self.directory["oauth_sources"]["peer"]["session_started_at"] = started
        self.claims[self.tokens["peer"]]["auth_time"] = started
        self.save()
        peer = await self.submit_as("peer", "peer")
        event = self.event()
        event.update(session_id="", issued_at=started - 1)
        await receive(self.console.store, event, "a" * 64)
        self.assertEqual((await view(self.console.store, a["id"]))["state"], "revoked")
        self.assertEqual(
            (await view(self.console.store, peer["id"]))["state"], "active"
        )
        await self.submit_as("peer", "new-login")
        del self.directory["oauth_sources"]["peer"]["session_started_at"]
        self.save()
        response = await self.client.post(
            "/api/os/runtime/runs",
            headers=self.headers_for("peer"),
            json={
                "profile_id": "review",
                "sample_id": "first-item",
                "goal": "review",
                "idempotency_key": "unknown",
            },
        )
        self.assertEqual(response.status_code, 403)

    async def test_event_before_admission_persists_across_restart_and_scope(self):
        event = self.event()
        await receive(self.console.store, event, "a" * 64)
        db = Database(str(self.db.engine.url))
        try:
            store = RuntimeStore(db, "one")
            self.assertTrue((await receive(store, event, "b" * 64))["duplicate"])
            other = RuntimeStore(db, "other")
            self.assertFalse((await receive(other, event, "b" * 64))["duplicate"])
        finally:
            await db.close()
        self.assertEqual(await self.count(), 2)
        self.console.provider.root.mkdir(exist_ok=True)
        target = self.root / "backup"
        await asyncio.to_thread(
            backup_sqlite, str(self.db.engine.url), self.console.provider.root, target
        )
        self.assertIsNotNone(await asyncio.to_thread(verify_backup, target))

    async def test_public_endpoint_requires_verification_before_any_mutation(self):
        run = await self.submit_as("a")
        env = {
            "DIOS_RUNTIME_LOGOUT_CONFIG": "/trusted/config",
            "DIOS_RUNTIME_LOGOUT_VERIFIER": "/trusted/binary",
        }
        url = "/api/os/runtime/identity/logout/provider"
        with (
            patch.dict("os.environ", env),
            patch(
                "app.runtime.logout_verifier.verify", new_callable=AsyncMock
            ) as verifier,
        ):
            verifier.side_effect = RuntimeErrorCode("LOGOUT_TOKEN_INVALID")
            response = await self.client.post(
                url, data={"logout_token": "unsigned"}, headers=self.headers_for("a")
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(await self.count(), 0)
            verifier.side_effect = None
            verifier.return_value = (self.event(), "a" * 64)
            response = await self.client.post(url, data={"logout_token": "signed"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(
                (await view(self.console.store, run["id"]))["state"], "revoked"
            )
            response = await self.client.post(url, json={"logout_token": "signed"})
            self.assertEqual(response.status_code, 415)
            response = await self.client.post(
                url,
                content="logout_token=a&logout_token=b",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(response.status_code, 400)
            response = await self.client.post(
                url,
                content="x" * 32769,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(response.status_code, 413)
