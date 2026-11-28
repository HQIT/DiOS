"""Independent directory package -> public administration CLI -> API/E2AG."""
import asyncio
import json
import sys
import time
import unittest

from dios_directory.mapping import plan, LIST, USER, GROUP
from app.runtime.principal_updates import projection
from app.runtime.database import RuntimeErrorCode
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.worker import Worker
from test_runtime_worker import FakeAdapter
import test_runtime_principals as fixtures


def listing(schema, rows):
    return {"schemas": [LIST], "totalResults": len(rows),
            "Resources": [dict(schemas=[schema], **row) for row in rows]}


class DirectoryBridgeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PrincipalTests.asyncSetUp
    asyncTearDown = fixtures.PrincipalTests.asyncTearDown
    save = fixtures.PrincipalTests.save
    submit_as = fixtures.PrincipalTests.submit_as
    headers_for = fixtures.PrincipalTests.headers_for
    get = fixtures.PrincipalTests.get
    finish = fixtures.PrincipalTests.finish

    def inputs(self):
        snapshot = {"version": 1, "source": "directory:test", "observed_at": time.time(),
                    "users": listing(USER, [{"id": "ua", "active": True}, {"id": "ub", "active": True}]),
                    "groups": listing(GROUP, [{"id": "engineering", "members": [{"value": "ua"}]},
                                              {"id": "sales", "members": [{"value": "ub"}]}])}
        permissions = self.directory["principals"]["a"]["profiles"]["review"]["permissions"]
        grant = {"review": {"actions": ["run.submit", "run.read", "artifact.read", "tool.read"],
                             "permissions": permissions}}
        mapping = {"version": 1, "source": "directory:test", "issuer": "fixture:issuer", "scope": "one",
                   "subjects": {"ua": "a", "ub": "b"}, "roles": {"engineering": grant, "sales": grant}}
        current = [projection(self.identities, pid, "one") for pid in ("a", "b")]
        return snapshot, mapping, current

    async def cli(self, *args):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.runtime.cli", "--database-url", str(self.console.db.engine.url),
            "--scope", "one", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        return process.returncode, stdout.decode(), stderr.decode()

    async def apply(self, patch):
        path = self.root / "directory-patch.json"
        path.write_text(json.dumps(patch["changes"])); path.chmod(0o600)
        return await self.cli("identity-update", "--catalog", str(self.identities), "--principal",
                              patch["principal_id"], "--revision", str(patch["expected_revision"]), "--file", str(path))

    async def test_two_departments_api_and_artifact_isolation(self):
        for patch in plan(*self.inputs())["patches"]:
            rc, out, err = await self.apply(patch); self.assertEqual(rc, 0, err + out)
        a = await self.submit_as("a"); await self.finish()
        b = await self.submit_as("b"); await self.finish()
        for own, other, pid in ((a, b, "a"), (b, a, "b")):
            self.assertEqual((await self.get(pid, "runs/" + other["id"])).status_code, 404)
            detail = (await self.get(pid, "runs/" + own["id"])).json()
            artifact = detail["artifacts"][0]
            url = "runs/" + own["id"] + "/artifacts/" + artifact["id"] + "/download"
            self.assertEqual((await self.get(pid, url)).status_code, 200)
            self.assertEqual((await self.get("b" if pid == "a" else "a", url)).status_code, 404)

    async def test_disable_replay_reenable_fences_old_execution_and_result(self):
        run = await self.submit_as("a")
        store = self.console.store
        await store.register_worker("worker", "instance", "host", 123)
        attempt = await store.claim("worker", "instance")
        raw = await store.get(run["id"])
        context = WorkAuthorizationContext("one", run["id"], attempt["id"], "worker", "instance",
            attempt["epoch"], attempt["authorization_epoch"], None, raw["input"]["work_authorization_ref"])
        await self.verifier(context, "execute")
        snapshot, mapping, current = self.inputs()
        snapshot["users"]["Resources"][0]["active"] = False
        change = plan(snapshot, mapping, current)["patches"][0]
        for replay in (False, True):
            rc, out, err = await self.apply(change); self.assertEqual(rc, 0, err + out)
            self.assertEqual(json.loads(out)["already_published"], replay)
        self.assertEqual((await self.get("a", "identity")).status_code, 401)
        self.assertEqual((await self.get("b", "identity")).status_code, 200)
        snapshot, mapping, current = self.inputs()
        rc, out, err = await self.apply(plan(snapshot, mapping, current)["patches"][0])
        self.assertEqual(rc, 0, err + out)
        self.assertEqual((await self.get("a", "identity")).status_code, 200)
        for action in ("execute", "accept_result"):
            with self.assertRaises(RuntimeErrorCode): await self.verifier(context, action)
        # Stable ownership permits historical read after re-enable; old authority stays dead.
        self.assertEqual((await self.get("a", "runs/" + run["id"])).status_code, 200)

    async def test_group_removal_blocks_new_and_queued_tasks(self):
        await self.submit_as("a")
        snapshot, mapping, current = self.inputs()
        snapshot["groups"]["Resources"][0]["members"] = []
        change = plan(snapshot, mapping, current)["patches"][0]
        rc, out, err = await self.apply(change); self.assertEqual(rc, 0, err + out)
        response = await self.client.post("/api/os/runtime/runs", headers=self.headers_for("a"), json={
            "profile_id": "review", "sample_id": "first-item", "goal": "review", "idempotency_key": "new"})
        self.assertEqual(response.status_code, 404)  # Unavailable profiles remain undiscoverable.
        adapter = FakeAdapter()
        await Worker(self.console.store, adapter, "review", self.spec, self.console.provider,
                     authorization_verifier=self.verifier).once()
        self.assertEqual(adapter.started, 0)

    async def test_stale_patch_cannot_restore_permissions(self):
        snapshot, mapping, current = self.inputs()
        old = plan(snapshot, mapping, current)["patches"][0]
        snapshot["users"]["Resources"][0]["active"] = False
        disable = plan(snapshot, mapping, current)["patches"][0]
        self.assertEqual((await self.apply(disable))[0], 0)
        rc, out, err = await self.apply(old)
        self.assertNotEqual(rc, 0)
        self.assertIn("VERSION_CONFLICT", out + err)
        self.assertEqual((await self.get("a", "identity")).status_code, 401)

    async def test_projection_cli_has_no_credentials_and_rejects_wrong_scope(self):
        rc, out, err = await self.cli("identity-policy", "--catalog", str(self.identities), "--principal", "a")
        self.assertEqual(rc, 0, err + out)
        self.assertEqual(json.loads(out), projection(self.identities, "a", "one"))
        for secret in (self.tokens["a"], "tokens", "sha256", "oauth_identity", "upstream"):
            self.assertNotIn(secret, out)
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_ACTION_DENIED"):
            projection(self.identities, "a", "other")

    async def test_online_directory_cli_deactivation_reenable_and_e2ag_fencing(self):
        from test_sync import DirectoryServer
        from dios_directory.journal import Journal
        snapshot, mapping, _ = self.inputs()
        server = DirectoryServer(snapshot)
        token = self.root / "directory-token"; token.write_text("fixture-token"); token.chmod(0o600)
        config = {"base_url": server.url, "token_file": str(token), "catalog": str(self.identities),
                  "runtime_command": [sys.executable, "-m", "app.runtime.cli", "--database-url", str(self.console.db.engine.url)],
                  "missing_resources": "disable"}
        config_path, mapping_path = self.root/"sync-config.json", self.root/"mapping.json"
        config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
        mapping_path.write_text(json.dumps(mapping))
        state = self.root / "directory-state"
        async def synchronize():
            child = await asyncio.create_subprocess_exec(sys.executable, "-m", "dios_directory.sync",
                "--config", str(config_path), "--mapping", str(mapping_path), "--state-dir", str(state),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(child.communicate(), 45)
            self.assertEqual(child.returncode, 0, out.decode()+err.decode())
            return json.loads(out)
        try:
            run = await self.submit_as("a")
            store = self.console.store
            await store.register_worker("online-worker", "boot", "host", 123)
            attempt = await store.claim("online-worker", "boot")
            raw = await store.get(run["id"])
            context = WorkAuthorizationContext("one", run["id"], attempt["id"], "online-worker", "boot",
                attempt["epoch"], attempt["authorization_epoch"], None, raw["input"]["work_authorization_ref"])
            await self.verifier(context, "execute")
            server.resources["Users/ua"]["active"] = False
            first = await synchronize(); self.assertEqual(first["state"], "synchronized")
            self.assertEqual((await self.get("a", "identity")).status_code, 401)
            self.assertEqual((await self.get("b", "identity")).status_code, 200)
            for action in ("execute", "accept_result"):
                with self.assertRaises(RuntimeErrorCode): await self.verifier(context, action)
            server.resources["Users/ua"]["active"] = True
            await synchronize()
            self.assertEqual((await self.get("a", "runs/" + run["id"])).status_code, 200)
            with self.assertRaises(RuntimeErrorCode): await self.verifier(context, "execute")
            self.assertEqual((await synchronize())["applied"], 0)
            journal = Journal(state)
            try:
                self.assertEqual(journal.status()["checkpoint"], 3)
                self.assertEqual(journal.status()["pending"], 0)
                self.assertGreater(journal.db.execute("SELECT count(*) FROM patches WHERE receipt IS NOT NULL").fetchone()[0], 0)
            finally: journal.close()
        finally: await asyncio.to_thread(server.close)
