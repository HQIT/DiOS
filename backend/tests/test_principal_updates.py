import asyncio
import json
import sys
from unittest.mock import patch
from sqlalchemy import select

from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.principal_updates import update
from app.runtime.principals import FilePrincipals
from app.runtime.delegation_schema import events
from app.runtime.work_auth_schema import work, grants, decisions
import test_delegation_reconciliation as fixtures


class PrincipalUpdateTests(fixtures.DelegationReconciliationTests):
    async def test_committed_update_precedes_competing_authorization_transaction(self):
        import threading
        from app.runtime.principal_updates import publish
        await self.direct_work()
        entered, release = threading.Event(), threading.Event()
        attempting = asyncio.Event()
        other = Database(str(self.db.engine.url))
        def held(*args):
            publish(*args)
            entered.set()
            if not release.wait(5):raise TimeoutError('qualification release deadline')
        async def competing():
            attempting.set()
            async with other.transaction('one') as c:
                return (await c.execute(select(work.c.state))).scalar_one()
        tasks=[]
        try:
            with patch('app.runtime.principal_updates.publish',side_effect=held):
                writer=asyncio.create_task(update(self.store,self.path,'issuer',1,{'enabled':False}));tasks.append(writer)
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                contender=asyncio.create_task(competing());tasks.append(contender)
                await asyncio.wait_for(attempting.wait(),2)
                release.set()
                await writer
                self.assertEqual(await contender,'revoked')
        finally:
            release.set()
            await asyncio.gather(*tasks,return_exceptions=True)
            await other.close()

    async def direct_work(self):
        binding = FilePrincipals(self.path).authenticate(self.token, "one")
        run = await self.store.submit("direct", {"principal": binding})
        async with self.db.transaction("one") as c:
            now = await self.db.now(c)
            await c.execute(
                work.insert().values(
                    id="direct-wa",
                    scope="one",
                    run_id=run["id"],
                    subject="subject",
                    actor="target",
                    policy_id="p",
                    policy_digest="a" * 64,
                    profile_id="profile",
                    profile_digest="b" * 64,
                    permissions={},
                    secret_refs=[],
                    state="active",
                    version=1,
                    authorization_epoch=1,
                    created_at=now,
                    expires_at=now + 600,
                )
            )
            await c.execute(
                grants.insert().values(
                    attempt_id="direct-attempt",
                    authorization_id="direct-wa",
                    run_id=run["id"],
                    worker_id="worker",
                    worker_instance="boot",
                    lease_epoch=1,
                    authorization_epoch=1,
                    session_id="session",
                    version=1,
                    state="active",
                    expires_at=now + 600,
                )
            )
        return binding

    async def test_disable_enable_keeps_old_binding_and_delegation_revoked(self):
        binding = await self.direct_work()
        first = await update(self.store, self.path, "issuer", 1, {"enabled": False})
        self.assertEqual(
            (
                first["revision"],
                first["revoked_delegations"],
                first["direct_work_revoked"],
            ),
            (2, 1, 1),
        )
        replay = await update(self.store, self.path, "issuer", 1, {"enabled": False})
        self.assertTrue(replay["already_published"])
        self.assertEqual(replay["revoked_delegations"], 0)
        await update(self.store, self.path, "issuer", 2, {"enabled": True})
        current = FilePrincipals(self.path)
        self.assertEqual(current.authenticate(self.token, "one")["revision"], 3)
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            current.current(binding)
        self.assertFalse((await self.manager.show("delegation"))["effective"])
        async with self.db.read("one") as c:
            self.assertEqual(
                (await c.execute(select(work.c.state))).scalar_one(), "revoked"
            )
            self.assertEqual(
                (await c.execute(select(grants.c.state))).scalar_one(), "revoked"
            )
            self.assertEqual(
                (await c.execute(select(events.c.operation))).scalars().all(),
                ["identity_revoked"],
            )
            self.assertEqual(
                (await c.execute(select(decisions.c.action))).scalars().all(),
                ["identity.update"],
            )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_invalid_patch_scope_and_stale_revision_do_not_write(self):
        original = self.path.read_bytes()
        for changes in ({"revision": 0}, {"enabled": "yes"}, {"profiles": None}):
            with self.assertRaises(RuntimeErrorCode):
                await update(self.store, self.path, "issuer", 1, changes)
        with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_CONFLICT"):
            await update(self.store, self.path, "issuer", 7, {"enabled": False})
        from app.runtime.store import RuntimeStore

        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_ACTION_DENIED"):
            await update(
                RuntimeStore(self.db, "other"),
                self.path,
                "issuer",
                1,
                {"enabled": False},
            )
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue((await self.manager.show("delegation"))["effective"])

    async def test_file_publication_failure_rolls_back_revocation(self):
        original = self.path.read_bytes()
        with patch(
            "app.runtime.principal_updates.publish", side_effect=OSError("disk failure")
        ):
            with self.assertRaises(OSError):
                await update(self.store, self.path, "issuer", 1, {"enabled": False})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue((await self.manager.show("delegation"))["effective"])
        self.assertEqual(list(self.root.glob(".identity-update-*")), [])

    async def test_failure_after_rename_retries_without_restoring_old_authority(self):
        binding = await self.direct_work()
        from app.runtime.principal_updates import publish

        def interrupted(*args):
            publish(*args)
            raise OSError("interruption after atomic replace")

        with patch("app.runtime.principal_updates.publish", side_effect=interrupted):
            with self.assertRaises(OSError):
                await update(self.store, self.path, "issuer", 1, {"enabled": False})
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            FilePrincipals(self.path).current(binding)
        # Restarted service uses the published revision to reconcile the rolled-back DB.
        db = Database(str(self.db.engine.url))
        from app.runtime.store import RuntimeStore

        try:
            result = await update(
                RuntimeStore(db, "one"), self.path, "issuer", 1, {"enabled": False}
            )
            self.assertTrue(result["already_published"])
            self.assertEqual(result["revoked_delegations"], 1)
        finally:
            await db.close()
        self.assertFalse((await self.manager.show("delegation"))["effective"])

    async def test_managed_writers_serialize_and_conflicting_changes_reject(self):
        results = await asyncio.gather(
            update(self.store, self.path, "issuer", 1, {"enabled": False}),
            update(self.store, self.path, "issuer", 1, {"enabled": True}),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        failure = next(x for x in results if isinstance(x, Exception))
        self.assertIsInstance(failure, RuntimeErrorCode)
        self.assertEqual(failure.code, "IDENTITY_UPDATE_BUSY")

    async def test_cli_update_returns_no_catalog_secrets(self):
        changes = self.root / "changes.json"
        changes.write_text(json.dumps({"enabled": False}))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.runtime.cli",
            "--database-url",
            str(self.db.engine.url),
            "--scope",
            "one",
            "identity-update",
            "--catalog",
            str(self.path),
            "--principal",
            "issuer",
            "--revision",
            "1",
            "--file",
            str(changes),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode())
        result = json.loads(stdout)
        self.assertEqual(result["revision"], 2)
        self.assertNotIn(self.token, json.dumps(result))
        self.assertNotIn("sha256", json.dumps(result))
