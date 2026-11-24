"""Independent source versions retain per-work E2AG authorization and history."""

import asyncio
import hashlib
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import test_runtime_oauth_source as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.oauth_source import OAuthSource
from app.runtime.principals import FilePrincipals


class MultiSourceTests(fixtures.SourceFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.OAuthPrincipalTests.asyncSetUp(self)
        self.override.stop()
        self.directory["oauth_sources"] = {"session-a-v1": self.reference}
        row = self.directory["principals"]["a"]
        del row["upstream"]
        row["source_refs"] = ["session-a-v1"]
        self.claims = {self.access: dict(self.response)}
        self.statuses = {}
        self.seen = []
        self.add_source("peer", "session-b", "v1", extra_lifetime=300)

        async def handle(request):
            token = parse_qs(request.content.decode())["token"][0]
            self.seen.append(token)
            return httpx.Response(
                self.statuses.get(token, 200),
                json=self.claims.get(token, {"active": False}),
            )

        self.transport = httpx.MockTransport(handle)
        actual = OAuthSource
        self.override = patch(
            "app.runtime.oauth_source.OAuthSource",
            side_effect=lambda config: actual(config, transport=self.transport),
        )
        self.override.start()
        self.save()

    asyncTearDown = fixtures.OAuthPrincipalTests.asyncTearDown
    save = fixtures.OAuthPrincipalTests.save
    headers_for = fixtures.OAuthPrincipalTests.headers_for
    submit_as = fixtures.OAuthPrincipalTests.submit_as
    get = fixtures.OAuthPrincipalTests.get
    finish = fixtures.OAuthPrincipalTests.finish
    context = fixtures.OAuthPrincipalTests.context

    def add_source(self, name, ref, version, extra_lifetime=0):
        token = "synthetic-independent-" + name + "-" + "y" * 64
        path = self.root / (name + ".token")
        path.write_text(token)
        path.chmod(0o600)
        source = dict(
            self.reference,
            ref=ref,
            version=version,
            path=str(path),
            sha256=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=self.expiry + extra_lifetime,
        )
        self.directory["oauth_sources"][name] = source
        self.directory["principals"]["a"]["source_refs"].append(name)
        self.tokens[name] = token
        self.claims[token] = dict(self.response, exp=source["expires_at"])
        return source

    async def binding(self, name):
        return await asyncio.to_thread(
            FilePrincipals(self.identities).authenticate, self.tokens[name], "one"
        )

    async def current(self, binding):
        return await asyncio.to_thread(FilePrincipals(self.identities).current, binding)

    async def test_only_selected_source_is_checked_and_no_other_credentials_leak(self):
        a = await self.binding("a")
        self.assertEqual(set(self.seen), {self.access})
        self.seen.clear()
        b = await self.binding("peer")
        self.assertEqual(set(self.seen), {self.tokens["peer"]})
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["upstream"], b["upstream"])
        response = await self.get("a", "identity")
        self.assertEqual(response.status_code, 200)
        for private in (
            self.access,
            self.tokens["peer"],
            str(self.root),
            "source_refs",
            "oauth_sources",
            "sha256",
        ):
            self.assertNotIn(private, response.text)

    async def test_append_refresh_version_preserves_work_binding_and_deadline(self):
        run = await self.submit_as("a", "original")
        context = await self.context(run)
        await self.verifier(context, "execute")
        original = await self.console.store.get(run["id"])
        before = await self.binding("a")
        self.add_source("refreshed", self.reference["ref"], "v2", extra_lifetime=600)
        self.directory["principals"]["a"]["source_refs"].reverse()
        self.save()
        await self.verifier(context, "execute")
        self.assertEqual(await self.binding("a"), before)
        self.assertEqual(
            (await self.console.store.get(run["id"]))["input"], original["input"]
        )
        from app.runtime.work_authorizations import view

        self.assertEqual(
            (await view(self.console.store, run["id"]))["expires_at"],
            run["authorization"]["expires_at"],
        )
        fresh = await self.submit_as("refreshed", "fresh")
        self.assertGreater(
            fresh["authorization"]["expires_at"], run["authorization"]["expires_at"]
        )
        self.assertEqual(
            (await self.get("refreshed", "runs/" + run["id"])).status_code, 200
        )

    async def test_selected_source_revocation_preserves_peer_and_cannot_revive_work(
        self,
    ):
        run = await self.submit_as("a", "revoked")
        context = await self.context(run)
        peer = await self.binding("peer")
        self.claims[self.access]["active"] = False
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.verifier(context, "execute")
        await self.current(peer)
        self.claims[self.access]["active"] = True
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.verifier(context, "execute")
        await self.current(peer)
        self.assertEqual((await self.get("peer", "runs/" + run["id"])).status_code, 200)

    async def test_removing_reference_revokes_only_bound_work(self):
        run = await self.submit_as("a", "removed")
        context = await self.context(run)
        peer = await self.binding("peer")
        self.directory["principals"]["a"]["source_refs"].remove("session-a-v1")
        self.save()
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.verifier(context, "execute")
        await self.current(peer)
        self.directory["principals"]["a"]["source_refs"].append("session-a-v1")
        self.save()
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.verifier(context, "accept_result")

    async def test_one_source_outage_preserves_grant_and_peer(self):
        run = await self.submit_as("a", "outage")
        context = await self.context(run)
        self.statuses[self.access] = 503
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PROVIDER_UNAVAILABLE"):
            await self.verifier(context, "execute")
        self.assertEqual((await self.get("peer", "identity")).status_code, 200)
        self.statuses[self.access] = 200
        await self.verifier(context, "execute")

    async def test_expired_source_does_not_expire_other_session(self):
        a = await self.binding("a")
        b = await self.binding("peer")
        with patch("app.runtime.oauth_source.time.time", return_value=self.expiry + 10):
            with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_EXPIRED"):
                await self.current(a)
            self.assertGreater((await self.current(b))["expires_at"], self.expiry)

    async def test_legacy_layout_conversion_preserves_existing_authority(self):
        row = self.directory["principals"]["a"]
        del row["source_refs"]
        row["upstream"] = self.reference
        del self.directory["oauth_sources"]
        self.save()
        binding = await self.binding("a")
        run = await self.submit_as("a", "legacy")
        context = await self.context(run)
        del row["upstream"]
        row["source_refs"] = ["session-a-v1"]
        self.directory["oauth_sources"] = {"session-a-v1": self.reference}
        self.save()
        self.assertEqual(await self.binding("a"), binding)
        await self.verifier(context, "execute")

    async def test_source_version_mutation_and_principal_revision_are_rejected(self):
        a = await self.binding("a")
        b = await self.binding("peer")
        self.reference["version"] = "replacement"
        self.save()
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.current(a)
        await self.current(b)
        self.directory["principals"]["a"]["revision"] += 1
        self.save()
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.current(b)

    async def test_empty_source_set_never_admits_unbound_identity(self):
        binding = await self.binding("a")
        self.directory["principals"]["a"]["source_refs"] = []
        self.save()
        for candidate in (
            binding,
            {k: v for k, v in binding.items() if k != "upstream"},
        ):
            with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
                await self.current(candidate)
        self.assertEqual((await self.get("a", "identity")).status_code, 401)

    async def test_invalid_ambiguous_and_cross_identity_mappings_fail_closed(self):
        original = deepcopy(self.directory)
        mutations = [
            lambda d: d["principals"]["a"]["source_refs"].append("peer"),
            lambda d: d["principals"]["a"]["source_refs"].append("missing"),
            lambda d: d["principals"]["a"].update(upstream=self.reference),
            lambda d: d["principals"]["a"].update(
                tokens=d["principals"]["b"]["tokens"]
            ),
            lambda d: d["principals"]["b"].update(source_refs=["peer"], tokens=[]),
            lambda d: d["oauth_sources"]["peer"].update(
                sha256=self.reference["sha256"]
            ),
            lambda d: d["oauth_sources"]["peer"].update(
                ref=self.reference["ref"], version=self.reference["version"]
            ),
            lambda d: d["oauth_sources"]["peer"].update(subject="different-person"),
            lambda d: d["oauth_sources"]["peer"].update(client_id="different-actor"),
        ]
        for mutation in mutations:
            self.directory = deepcopy(original)
            mutation(self.directory)
            self.save()
            with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PROVIDER_UNAVAILABLE"):
                FilePrincipals(self.identities).document()
        self.directory = original

    async def test_two_sources_submit_independent_tasks_and_history_remains_accessible(
        self,
    ):
        a = await self.submit_as("a", "a")
        await self.finish()
        b = await self.submit_as("peer", "b")
        await self.finish()
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual((await self.get("peer", "runs/" + a["id"])).status_code, 200)
        self.assertEqual((await self.get("a", "runs/" + b["id"])).status_code, 200)
        self.assertEqual((await self.get("b", "runs/" + a["id"])).status_code, 404)
        raw = await self.console.store.get(a["id"])
        self.assertEqual(raw["input"]["principal"]["upstream"]["version"], "v1")
        self.assertNotIn(self.tokens["peer"], json.dumps(raw))
