"""Managed source admission, create-only storage and E2AG fault regressions."""

import asyncio
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import test_runtime_oauth_source as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.openbao_secrets import OpenBaoSecrets
from app.runtime.principals import FilePrincipals


class IntakeTests(fixtures.SourceFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.OAuthPrincipalTests.asyncSetUp(self)
        self.response["scope"] = "openid review"
        row = self.directory["principals"]["a"]
        del row["upstream"]
        row["source_refs"] = []
        row["oauth_identity"] = {
            "issuer": self.config["issuer"],
            "subject": "upstream-alice",
            "client_id": "portal",
            "required_scopes": ["openid"],
        }
        self.reader = self.root / "reader"
        self.reader.write_text("reader")
        self.reader.chmod(0o600)
        self.writer = self.root / "writer"
        self.writer.write_text("writer")
        self.writer.chmod(0o600)
        store = {
            "address": "http://127.0.0.1:18888",
            "token_file": str(self.reader),
            "timeout_seconds": 0.2,
        }
        self.config["secret_store"] = store
        self.directory["oauth_intake"] = {
            "writer": dict(store, token_file=str(self.writer)),
            "mount": "sources",
            "prefix": "admission",
            "timeout_seconds": 2,
            "max_source_ttl_seconds": 3600,
        }
        self.records = {}
        self.deleted = set()
        self.posts = 0
        self.lock = threading.Lock()
        self.fail_store = False
        self.drop_reply = False

        def handle(request):
            key = request.url.path
            if self.fail_store:
                return httpx.Response(503)
            with self.lock:
                if request.method == "POST":
                    if request.headers["X-Vault-Token"] != "writer":
                        return httpx.Response(403)
                    self.assertEqual(json.loads(request.content)["options"], {"cas": 0})
                    if key in self.records or key in self.deleted:
                        return httpx.Response(400)
                    self.records[key] = json.loads(request.content)["data"]
                    self.posts += 1
                    if self.drop_reply:
                        self.drop_reply = False
                        raise httpx.ReadTimeout("Lost write receipt", request=request)
                    return httpx.Response(200, json={"data": {"version": 1}})
                self.assertEqual(request.url.params["version"], "1")
                if key not in self.records or key in self.deleted:
                    return httpx.Response(404)
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "data": copy.deepcopy(self.records[key]),
                            "metadata": {
                                "version": 1,
                                "destroyed": False,
                                "deletion_time": "",
                            },
                        }
                    },
                )

        self.secret_transport = httpx.MockTransport(handle)
        actual = OpenBaoSecrets
        self.store_override = patch(
            "app.runtime.openbao_secrets.OpenBaoSecrets",
            side_effect=lambda config, **_: actual(
                config, transport=self.secret_transport
            ),
        )
        self.store_override.start()
        self.intake_store_override = patch(
            "app.runtime.source_intake.OpenBaoSecrets",
            side_effect=lambda config: actual(config, transport=self.secret_transport),
        )
        self.intake_store_override.start()
        # The intake uses the same RFC 7662 fixture as ongoing source checks.
        from app.runtime.oauth_source import OAuthSource

        self.inspect_override = patch(
            "app.runtime.source_intake.OAuthSource",
            side_effect=lambda config: OAuthSource(config),
        )
        self.inspect_override.start()
        self.save()

    async def asyncTearDown(self):
        self.inspect_override.stop()
        self.intake_store_override.stop()
        self.store_override.stop()
        await fixtures.OAuthPrincipalTests.asyncTearDown(self)

    save = fixtures.OAuthPrincipalTests.save
    headers_for = fixtures.OAuthPrincipalTests.headers_for
    submit_as = fixtures.OAuthPrincipalTests.submit_as
    get = fixtures.OAuthPrincipalTests.get
    finish = fixtures.OAuthPrincipalTests.finish
    context = fixtures.OAuthPrincipalTests.context

    async def authenticate(self):
        return await asyncio.to_thread(
            FilePrincipals(self.identities).authenticate, self.access, "one"
        )

    async def test_verified_source_enrolls_without_mutating_catalog_and_executes(self):
        original = self.identities.read_bytes()
        binding = await self.authenticate()
        self.assertEqual(self.identities.read_bytes(), original)
        self.assertTrue(binding["upstream"]["ref"].startswith("enrolled/"))
        self.assertEqual(self.posts, 1)
        run = await self.submit_as("a")
        await self.finish()
        raw = await self.console.store.get(run["id"])
        self.assertEqual(raw["state"], "succeeded")
        self.assertEqual(raw["input"]["principal"], binding)
        self.assertNotIn(self.access, json.dumps(raw))
        self.assertNotIn(str(self.writer), json.dumps(raw))
        self.assertEqual(self.posts, 1)

    async def test_parallel_retries_share_one_atomic_record(self):
        bindings = await asyncio.gather(*(self.authenticate() for _ in range(8)))
        self.assertTrue(all(x == bindings[0] for x in bindings))
        self.assertEqual(self.posts, 1)
        self.assertEqual(len(self.records), 1)

    async def test_registered_source_works_when_writer_credential_is_unavailable(self):
        binding = await self.authenticate()
        self.writer.unlink()
        self.assertEqual(await self.authenticate(), binding)
        self.assertEqual(self.posts, 1)

    async def test_lost_write_receipt_recovers_original_record(self):
        self.drop_reply = True
        binding = await self.authenticate()
        self.assertEqual(await self.authenticate(), binding)
        self.assertEqual(self.posts, 1)

    async def test_bad_identity_scope_and_lifetime_never_store_credentials(self):
        original = dict(self.response)
        for changes in (
            {"sub": "other-user"},
            {"client_id": "other-client"},
            {"aud": ["other-api"]},
            {"iss": "https://other.example"},
            {"scope": "review"},
            {"active": False},
            {"exp": time.time() + 7200},
        ):
            self.response = dict(original, **changes)
            with self.assertRaises(RuntimeErrorCode):
                await self.authenticate()
            self.assertFalse(self.records)
        self.response = original
        self.directory["principals"]["a"]["enabled"] = False
        self.save()
        with self.assertRaises(RuntimeErrorCode):
            await self.authenticate()
        self.assertFalse(self.records)

    async def test_source_scope_narrowing_revokes_existing_work(self):
        run = await self.submit_as("a")
        context = await self.context(run)
        self.response["scope"] = "review"
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.verifier(context, "execute")
        self.response["scope"] = "openid review"
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.verifier(context, "execute")

    async def test_deleted_record_cannot_be_reenrolled_or_revive_work(self):
        run = await self.submit_as("a")
        context = await self.context(run)
        self.deleted.update(self.records)
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.verifier(context, "execute")
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PROVIDER_UNAVAILABLE"):
            await self.authenticate()
        self.assertEqual(self.posts, 1)
        self.deleted.clear()
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.verifier(context, "execute")

    async def test_store_outage_retains_work_and_recovers(self):
        run = await self.submit_as("a")
        context = await self.context(run)
        self.fail_store = True
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PROVIDER_UNAVAILABLE"):
            await self.verifier(context, "execute")
        self.fail_store = False
        await self.verifier(context, "execute")
        self.assertEqual(self.posts, 1)

    async def test_corrupt_record_and_changed_mapping_are_not_adopted(self):
        binding = await self.authenticate()
        key = next(iter(self.records))
        original = copy.deepcopy(self.records[key])
        self.records[key]["principal"]["id"] = "other"
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.authenticate()
        self.records[key] = original
        self.directory["principals"]["a"]["oauth_identity"]["required_scopes"] = [
            "openid",
            "review",
        ]
        self.save()
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await asyncio.to_thread(FilePrincipals(self.identities).current, binding)
        newer = await self.authenticate()
        self.assertNotEqual(newer["upstream"], binding["upstream"])

    async def test_refresh_creates_new_source_and_preserves_old_deadline(self):
        run = await self.submit_as("a")
        context = await self.context(run)
        binding = (await self.console.store.get(run["id"]))["input"]["principal"]
        self.access += "-refresh"
        self.response["exp"] += 300
        self.tokens["a"] = self.access
        fresh = await self.authenticate()
        self.assertNotEqual(fresh["upstream"]["ref"], binding["upstream"]["ref"])
        await self.verifier(context, "execute")
        from app.runtime.work_authorizations import view

        self.assertEqual(
            (await view(self.console.store, run["id"]))["expires_at"],
            run["authorization"]["expires_at"],
        )
        self.assertEqual(self.posts, 2)

    async def test_ambiguous_mapping_and_unsafe_configuration_fail_closed(self):
        original = copy.deepcopy(self.directory)
        for change in ("duplicate", "store", "ttl", "scope", "mixed"):
            self.directory = copy.deepcopy(original)
            if change == "duplicate":
                self.directory["principals"]["duplicate"] = copy.deepcopy(
                    self.directory["principals"]["a"]
                )
            if change == "store":
                self.directory["oauth_intake"]["writer"]["address"] = (
                    "http://127.0.0.1:9999"
                )
            if change == "ttl":
                self.directory["oauth_intake"]["max_source_ttl_seconds"] = float("inf")
            if change == "scope":
                self.directory["principals"]["a"]["oauth_identity"][
                    "required_scopes"
                ] = ["a b"]
            if change == "mixed":
                self.directory["principals"]["a"]["tokens"] = self.directory[
                    "principals"
                ]["b"]["tokens"]
            self.save()
            with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PROVIDER_UNAVAILABLE"):
                FilePrincipals(self.identities).document()
