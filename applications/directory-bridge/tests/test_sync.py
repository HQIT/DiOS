import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlsplit

from dios_directory.journal import Journal
from dios_directory.scim import ScimClient, SyncError
from dios_directory.sync import sync_once
from test_mapping import fixture


class DirectoryServer:
    def __init__(self, snapshot):
        self.resources = {kind + "/" + row["id"]: copy.deepcopy(row)
                          for kind, section in (("Users", "users"), ("Groups", "groups"))
                          for row in snapshot[section]["Resources"]}
        self.calls, self.not_modified, self.status, self.headers = [], 0, 200, {}
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                key = unquote(urlsplit(self.path).path).removeprefix("/scim/")
                state.calls.append(key)
                if self.headers.get("Authorization") != "Bearer fixture-token":
                    self.send_response(401); self.end_headers(); return
                if state.status != 200:
                    self.send_response(state.status)
                    for k, v in state.headers.items(): self.send_header(k, v)
                    self.end_headers(); return
                if key not in state.resources:
                    self.send_response(404); self.send_header("Content-Type", "application/scim+json"); self.end_headers()
                    self.wfile.write(json.dumps({"schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"], "status": "404"}).encode()); return
                value = state.resources[key]
                raw = json.dumps(value, sort_keys=True).encode()
                import hashlib
                etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
                if self.headers.get("If-None-Match") == etag:
                    state.not_modified += 1
                    self.send_response(304); self.send_header("ETag", etag); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/scim+json")
                self.send_header("ETag", etag); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/scim"

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)


class Runtime:
    def __init__(self, rows):
        self.rows = {r["principal_id"]: copy.deepcopy(r) for r in rows}
        self.calls, self.fail, self.crash = [], False, False

    async def projection(self, pid): return copy.deepcopy(self.rows[pid])

    async def apply(self, patch):
        if self.fail: raise SyncError("RUNTIME_RESPONSE_UNCERTAIN")
        self.calls.append(copy.deepcopy(patch))
        row = self.rows[patch["principal_id"]]
        replay = row["revision"] == patch["expected_revision"] + 1 and all(row[k] == v for k,v in patch["changes"].items())
        if not replay:
            if row["revision"] != patch["expected_revision"]: raise SyncError("IDENTITY_VERSION_CONFLICT")
            row.update(copy.deepcopy(patch["changes"]), revision=patch["expected_revision"]+1)
        if self.crash:
            self.crash = False
            raise InterruptedError("simulated lost receipt")
        return {"principal_id": row["principal_id"], "revision": row["revision"],
                "enabled": row["enabled"], "already_published": replay}


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        snapshot, self.mapping, current = fixture()
        self.server = DirectoryServer(snapshot)
        self.token = self.root / "token"; self.token.write_text("fixture-token"); self.token.chmod(0o600)
        self.config = {"base_url": self.server.url, "token_file": str(self.token), "runtime_command": [sys.executable],
                       "catalog": "/trusted/catalog", "missing_resources": "disable"}
        self.state, self.now = self.root / "state", 1000
        self.runtime = Runtime(current)

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.close); self.temp.cleanup()

    async def once(self):
        return await sync_once(self.config, self.mapping, self.state, runtime=self.runtime, clock=lambda: self.now)

    async def test_conditional_reads_noop_and_private_minimal_cache(self):
        self.server.resources["Users/u1"]["emails"] = ["private@example.invalid"]
        first = await self.once(); self.assertEqual(first["applied"], 2)
        second = await self.once(); self.assertEqual(second["applied"], 0)
        self.assertEqual(self.server.not_modified, 4)
        self.assertEqual(len(self.runtime.calls), 2)
        journal = Journal(self.state)
        try:
            self.assertNotIn("private@example", json.dumps(journal.cached("Users/u1")))
            self.assertEqual(journal.status()["checkpoint"], 2)
        finally: journal.close()
        self.assertEqual((self.state / "sync.sqlite").stat().st_mode & 0o777, 0o600)

    async def test_deactivation_removal_deletion_and_reenable(self):
        await self.once()
        self.server.resources["Users/u1"]["active"] = False
        self.server.resources["Groups/sales"]["members"] = []
        self.assertEqual((await self.once())["applied"], 2)
        self.assertFalse(self.runtime.rows["p1"]["enabled"])
        self.assertEqual(self.runtime.rows["p2"]["profiles"], {})
        del self.server.resources["Users/u2"]
        await self.once(); self.assertFalse(self.runtime.rows["p2"]["enabled"])
        self.server.resources["Users/u1"]["active"] = True
        await self.once(); self.assertTrue(self.runtime.rows["p1"]["enabled"])
        self.assertEqual(self.runtime.rows["p1"]["revision"], 4)

    async def test_outage_and_rate_limit_preserve_checkpoint_then_resume(self):
        await self.once(); self.server.status = 429; self.server.headers = {"Retry-After": "20"}
        failed = await self.once()
        self.assertEqual((failed["checkpoint"], failed["next_retry_at"]), (1, 1020))
        self.assertEqual(failed["last_error"], "SCIM_RATE_LIMITED")
        calls = len(self.server.calls)
        self.assertEqual((await self.once())["state"], "cooldown")
        self.assertEqual(len(self.server.calls), calls)
        self.now = 1020; self.server.status = 503
        self.assertEqual((await self.once())["last_error"], "SCIM_UNAVAILABLE")
        self.now = 1040; self.server.status = 200
        result = await self.once()
        self.assertEqual(result["state"], "synchronized"); self.assertEqual(result["failures"], 0)

    async def test_lost_receipt_restart_replays_same_expected_revision(self):
        self.runtime.crash = True
        failed = await self.once(); self.assertEqual(failed["pending"], 2)
        self.assertEqual(failed["checkpoint"], 0)
        self.now += 5
        result = await self.once()
        self.assertEqual(result["state"], "synchronized")
        self.assertEqual(self.runtime.rows["p1"]["revision"], 2)
        self.assertEqual(self.runtime.calls[0], self.runtime.calls[1])

    async def test_pending_grant_not_replayed_after_directory_revocation(self):
        self.runtime.fail = True; await self.once()
        self.server.resources["Users/u1"]["active"] = False
        self.runtime.fail = False; self.now += 5
        await self.once()
        a_calls = [p for p in self.runtime.calls if p["principal_id"] == "p1"]
        self.assertEqual(len(a_calls), 1); self.assertFalse(a_calls[0]["changes"]["enabled"])
        journal = Journal(self.state)
        try:
            self.assertEqual(journal.db.execute("SELECT count(*) FROM patches WHERE state='superseded'").fetchone()[0], 1)
        finally: journal.close()

    async def test_default_missing_rejects_and_partial_read_never_publishes(self):
        self.config["missing_resources"] = "reject"
        del self.server.resources["Users/u2"]
        result = await self.once()
        self.assertEqual(result["last_error"], "SCIM_MISSING_RESOURCE_REQUIRES_POLICY")
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(result["checkpoint"], 0)

    async def test_missing_group_members_and_nested_groups_are_errors(self):
        self.server.resources["Groups/engineering"].pop("members")
        self.assertEqual((await self.once())["last_error"], "SCIM_COMPLETE_MEMBERS_REQUIRED")
        self.server.resources["Groups/engineering"]["members"] = [{"value": "sales", "type": "Group"}]
        self.now += 5
        self.assertEqual((await self.once())["last_error"], "NESTED_GROUP_UNSUPPORTED")
        self.assertEqual(self.runtime.calls, [])

    async def test_duplicate_trigger_and_inherited_owner_lock(self):
        owner = Journal(self.state)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], pass_fds=(owner.lock,))
        owner.close()
        try:
            self.assertEqual((await self.once())["state"], "busy")
            self.assertEqual(self.server.calls, [])
        finally:
            child.terminate(); await asyncio.to_thread(child.wait)
        self.assertEqual((await self.once())["state"], "synchronized")

    async def test_auth_failure_and_redirect_do_not_clear_permissions(self):
        await self.once()
        for status, code in ((401, "SCIM_AUTH_DENIED"), (403, "SCIM_AUTH_DENIED"), (302, "SCIM_RESPONSE_REJECTED")):
            self.server.status = status; self.now += 300
            result = await self.once()
            self.assertEqual(result["last_error"], code)
            self.assertEqual(result["checkpoint"], 1)
            self.assertEqual(len(self.runtime.calls), 2)

    async def test_namespace_and_expired_observation_reject(self):
        await self.once(); self.mapping["subjects"]["u3"] = "p3"
        self.assertEqual((await self.once())["last_error"], "SYNC_NAMESPACE_CHANGED")
        self.mapping["subjects"].pop("u3"); self.now += 300
        self.config["max_age_seconds"] = 1
        original = self.runtime.projection
        async def slow(pid):
            self.now += 2
            return await original(pid)
        self.runtime.projection = slow
        self.assertEqual((await self.once())["last_error"], "STALE_DIRECTORY_SNAPSHOT")
        self.assertEqual(len(self.runtime.calls), 2)

    async def test_total_timeout_invalid_304_and_unconfirmed_deletion(self):
        import httpx
        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={})
        for handler, code in ((slow, "SCIM_UNAVAILABLE"),
                              (lambda r: httpx.Response(304), "UNEXPECTED_NOT_MODIFIED"),
                              (lambda r: httpx.Response(404, json={}), "SCIM_DELETION_UNCONFIRMED"),
                              (lambda r: httpx.Response(200, content=b'{"active":false,"active":true}'), "DUPLICATE_JSON_KEY")):
            client = ScimClient(dict(self.config, timeout_seconds=.05), transport=httpx.MockTransport(handler))
            try:
                with self.assertRaisesRegex(ValueError, code): await client.read("Users", "u1")
            finally: await client.close()

    async def test_runtime_command_retains_lock_after_parent_releases_it(self):
        from dios_directory.sync import RuntimeCLI
        owner = Journal(self.state)
        marker, release = self.root/"started", self.root/"release"
        script = ("from pathlib import Path\nimport time,json\n"
                  f"Path({str(marker)!r}).touch()\n"
                  f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
                  "print(json.dumps({'finished':True}))")
        runtime = RuntimeCLI(dict(self.config, runtime_command=[sys.executable, "-c", script]),
                             "one", self.state, owner_fd=owner.lock)
        task = asyncio.create_task(runtime.call())
        closed = False
        try:
            async with asyncio.timeout(3):
                while not marker.exists(): await asyncio.sleep(.01)
            owner.close(); closed = True
            self.assertEqual((await self.once())["state"], "busy")
            release.touch(); self.assertEqual(await task, {"finished": True})
            self.assertEqual((await self.once())["state"], "synchronized")
        finally:
            if not closed: owner.close()
            release.touch()
            await asyncio.wait_for(task, 3)


if __name__ == "__main__": unittest.main()
