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
