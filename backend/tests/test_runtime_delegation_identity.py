"""Authenticated API derivation with native OPA and durable issuer fences."""

import hashlib
import json
import secrets
import time
from unittest.mock import patch
from fastapi import FastAPI
import httpx
from sqlalchemy import select
from app.api.os import runtime_tasks
from app.middleware.access_token import AccessTokenMiddleware
from app.runtime.database import RuntimeErrorCode
from app.runtime.delegation_identity_schema import issuers
from app.runtime.delegation_schema import links
from app.runtime.logout_events import receive
from app.runtime.principals import FilePrincipals
from app.runtime.work_authorizations import view
from app.services.task_console import TaskConsole
import test_runtime_delegation_lifecycle as fixtures


class DelegationIdentityTests(fixtures.DelegationLifecycleTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.identity_path = self.root / "principals.json"
        self.tokens = {name: secrets.token_urlsafe(32) for name in ("issuer", "peer")}
        now = time.time()
        self.directory = {
            "version": 1,
            "issuer": "fixture:delegation",
            "audience": "dios-runtime",
            "principals": {},
        }
        for name, token in self.tokens.items():
            self.directory["principals"][name] = dict(
                subject="subject:" + name,
                actor=self.chain[0]["actor_ref"] if name == "issuer" else "actor:peer",
                scope=self.scope,
                enabled=True,
                revision=1,
                expires_at=now + 1200,
                tokens=[
                    dict(
                        sha256=hashlib.sha256(token.encode()).hexdigest(),
                        enabled=True,
                        not_before=now - 10,
                        expires_at=now + 1200,
                    )
                ],
                profiles={
                    "test-profile": dict(
                        actions=[
                            "delegation.issue",
                            "delegation.read",
                            "delegation.revoke",
                        ],
                        permissions=self.permissions,
                    )
                },
            )
        self.save_identity()
        policy = json.loads(self.policy.path.read_text())
        policy["identity_catalog"] = str(self.identity_path)
        self.policy.path.write_text(json.dumps(policy))
        self.console = TaskConsole(
            self.db, self.scope, self.root, self.provider.root, policy=self.policy
        )
        self.app = FastAPI()
        self.app.include_router(runtime_tasks.router, prefix="/api/os")
        self.app.add_middleware(AccessTokenMiddleware)
        self.app.state.runtime_console = self.console
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.enable_patch = patch.object(
            runtime_tasks.settings, "runtime_console_enabled", True
        )
        self.enable_patch.start()
        self.operator_patch = patch.object(
            runtime_tasks.settings, "access_token", "test-operator"
        )
        self.operator_patch.start()
        self.body = dict(
            idempotency_key="api-child",
            parent_ref=self.chain[0]["id"],
            profile_id="test-profile",
            actor=self.chain[-1]["actor_ref"],
            permissions=self.permissions,
            expires_at=self.deadline - 50,
        )

    async def asyncTearDown(self):
        if hasattr(self, "http"):
            await self.http.aclose()
        if hasattr(self, "operator_patch"):
            self.operator_patch.stop()
            self.enable_patch.stop()
        await super().asyncTearDown()

    def save_identity(self):
        self.identity_path.write_text(json.dumps(self.directory))

    def headers(self, name="issuer"):
        return {"Authorization": "Bearer " + self.tokens[name]}

    async def issue_api(self, body=None):
        response = await self.http.post(
            "/api/os/runtime/delegations",
            headers=self.headers(),
            json=body or self.body,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def bind_api_work(self, child, key="api-work"):
        policy = json.loads(self.policy.path.read_text())
        policy["profiles"]["test-profile"]["delegation_ref"] = child["id"]
        self.policy.path.write_text(json.dumps(policy))
        descriptor = self.policy.admission("test-profile", self.spec.fingerprint(), [])
        return await self.store.submit(
            key,
            dict(
                runtime_profile="test-profile",
                profile_digest=self.spec.fingerprint(),
                secret_refs=[],
            ),
            authorization=descriptor,
        )

    async def test_api_binding_idempotency_revoke_and_history(self):
        child = await self.issue_api()
        self.assertEqual((await self.issue_api())["id"], child["id"])
        path = "/api/os/runtime/delegations/" + child["id"]
        response = await self.http.get(path, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["events"][0]["details"]["authenticated_issuer"]["id"],
            "issuer",
        )
        self.assertNotIn(str(self.identity_path), response.text)
        async with self.db.read(self.scope) as c:
            source = (
                (
                    await c.execute(
                        select(issuers).where(issuers.c.delegation_ref == child["id"])
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(
                source["principal"],
                FilePrincipals(self.identity_path).authenticate(
                    self.tokens["issuer"], self.scope
                ),
            )
        run = await self.bind_api_work(child)
        for _ in range(2):
            response = await self.http.post(
                path + "/revoke", headers=self.headers(), json={"expected_version": 1}
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await view(self.store, run["id"]))["state"], "revoked")

    async def test_api_denies_identity_injection_peer_operator_and_expansion(self):
        for key, value in [
            ("issuer_actor", "forged"),
            ("subject", "forged"),
            ("policy_evaluator", self.config),
            ("issuer_source", {}),
        ]:
            response = await self.http.post(
                "/api/os/runtime/delegations",
                headers=self.headers(),
                json=dict(self.body, **{key: value}),
            )
            self.assertEqual(response.status_code, 422, response.text)
        for headers in (
            {"Authorization": "Bearer test-operator"},
            self.headers("peer"),
            {},
        ):
            response = await self.http.post(
                "/api/os/runtime/delegations", headers=headers, json=self.body
            )
            self.assertIn(response.status_code, (401, 403))
        child = await self.issue_api()
        self.assertEqual(
            (
                await self.http.get(
                    "/api/os/runtime/delegations/" + child["id"],
                    headers=self.headers("peer"),
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            (
                await self.http.post(
                    "/api/os/runtime/delegations/" + child["id"] + "/revoke",
                    headers=self.headers("peer"),
                    json={"expected_version": 1},
                )
            ).status_code,
            404,
        )
        body = dict(
            self.body,
            idempotency_key="expansion",
            permissions=self.permissions
            + [dict(self.permission, action="shell.execute")],
        )
        self.assertEqual(
            (
                await self.http.post(
                    "/api/os/runtime/delegations", headers=self.headers(), json=body
                )
            ).status_code,
            403,
        )

    async def test_issuer_recheck_after_opa_wait_rolls_back_issuance(self):
        from app.runtime.policy_evaluator import OpaEvaluator

        original = OpaEvaluator.evaluate

        async def revoke_while_waiting(instance, *args):
            result = await original(instance, *args)
            self.directory["principals"]["issuer"]["enabled"] = False
            self.save_identity()
            return result

        with patch.object(OpaEvaluator, "evaluate", revoke_while_waiting):
            response = await self.http.post(
                "/api/os/runtime/delegations", headers=self.headers(), json=self.body
            )
        self.assertEqual(response.status_code, 403, response.text)
        async with self.db.read(self.scope) as c:
            self.assertEqual((await c.execute(select(issuers))).all(), [])

    async def test_identity_revocation_and_outage_block_delegated_work(self):
        child = await self.issue_api()
        self.identity_path.write_text("{broken")
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PROVIDER_UNAVAILABLE"):
            await self.bind_api_work(child)
        self.save_identity()
        self.assertTrue((await self.manager.show(child['id']))['effective'])
        self.directory["principals"]["issuer"]["revision"] = 2
        self.save_identity()
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.bind_api_work(child)
        self.directory["principals"]["issuer"]["revision"] = 1
        self.save_identity()
        self.assertFalse((await self.manager.show(child['id']))['effective'])
        with self.assertRaisesRegex(RuntimeErrorCode, "DELEGATION_REVOKED"):
            await self.bind_api_work(child)
        replacement = await self.issue_api(dict(self.body, idempotency_key='replacement-child'))
        self.assertEqual((await self.bind_api_work(replacement))["state"], "queued")

    async def test_logout_fences_issuer_dependency_and_preserves_peer(self):
        child = await self.issue_api()
        run = await self.bind_api_work(child)
        # Verified logout receiver is qualified separately with native OIDC. Here
        # the captured source metadata exercises SQL dependency fan-out.
        upstream = dict(
            issuer="https://idp.example",
            client_id="portal",
            subject="source-issuer",
            session_id="issuer-session",
            session_started_at=int(time.time()) - 10,
        )
        async with self.db.transaction(self.scope) as c:
            source = (
                (
                    await c.execute(
                        select(issuers).where(issuers.c.delegation_ref == child["id"])
                    )
                )
                .mappings()
                .one()
            )
            await c.execute(
                issuers.update()
                .where(issuers.c.delegation_ref == child["id"])
                .values(principal=dict(source["principal"], upstream=upstream))
            )
        event = dict(
            issuer=upstream["issuer"],
            client_id="portal",
            subject="source-issuer",
            session_id="issuer-session",
            event_id="issuer-logout",
            issued_at=int(time.time()),
            expires_at=int(time.time()) + 60,
        )
        result = await receive(self.store, event, "a" * 64)
        self.assertEqual(result["revoked_work_count"], 1)
        self.assertTrue((await receive(self.store, event, "a" * 64))["duplicate"])
        self.assertEqual((await view(self.store, run["id"]))["state"], "revoked")
        self.assertEqual((await view(self.store, self.run["id"]))["state"], "active")
        described = await view(self.store, run["id"])
        leaf = described["delegation"]["chain"][-1]
        self.assertEqual(leaf["source_event"]["id"], result["event_id"])
        self.assertEqual(leaf["source_event"]["event_count"], 1)
        self.assertEqual(leaf["revocation"]["details"]["logout_event_id"], result["event_id"])
        self.assertEqual(leaf["revocation"]["operation"], "identity_revoked")
        # An older delivery adds provenance without another authority transition.
        older = dict(event, event_id="issuer-delayed", issued_at=event["issued_at"] - 1)
        await receive(self.store, older, "b" * 64)
        after = (await view(self.store, run["id"]))["delegation"]["chain"][-1]
        self.assertEqual(after["source_event"]["id"], result["event_id"])
        self.assertEqual(after["source_event"]["event_count"], 2)
        self.assertEqual(after["revocation"], leaf["revocation"])
        response = await self.http.get("/api/os/runtime/delegations/" + child["id"], headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["effective"])
        self.assertEqual(response.json()["chain"][-1]["source_event"], after["source_event"])
        async with self.db.read(self.scope) as c:
            row = (
                (await c.execute(select(links).where(links.c.id == child["id"])))
                .mappings()
                .one()
            )
            self.assertEqual((row["state"], row["authorization_epoch"]), ("revoked", 2))

    async def test_observed_issuer_permission_removal_fences_existing_work(self):
        from app.runtime.authorization import WorkAuthorizationContext
        from app.runtime.work_authorizations import WorkAuthorizationVerifier
        child=await self.issue_api();run=await self.bind_api_work(child)
        await self.store.register_worker('issuer-test-worker','boot','host',123)
        attempt=await self.store.claim('issuer-test-worker','boot')
        self.assertEqual(attempt['run_id'],run['id'])
        context=WorkAuthorizationContext(self.scope,run['id'],attempt['id'],'issuer-test-worker','boot',
            attempt['epoch'],attempt['authorization_epoch'],None,run['input']['work_authorization_ref'])
        verifier=WorkAuthorizationVerifier(self.store,self.policy)
        await verifier(context,'execute')
        actions=self.directory['principals']['issuer']['profiles']['test-profile']['actions']
        actions.remove('delegation.issue');self.save_identity()
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await verifier(context,'execute')
        self.assertEqual((await view(self.store,run['id']))['state'],'revoked')
        self.assertFalse((await self.manager.show(child['id']))['effective'])
        actions.append('delegation.issue');self.save_identity()
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await verifier(context,'execute')
        with self.assertRaisesRegex(RuntimeErrorCode,'DELEGATION_REVOKED'):
            await self.bind_api_work(child, 'new-work-after-restore')

    async def test_backup_restores_authenticated_issuer_binding(self):
        import asyncio
        from app.runtime.recovery import backup_sqlite,restore_sqlite
        from app.runtime.database import Database
        child=await self.issue_api()
        async with self.db.read(self.scope) as c:
            before=dict((await c.execute(select(issuers).where(issuers.c.delegation_ref==child['id']))).mappings().one())
        destination=self.root/'issuer-backup'
        await asyncio.to_thread(backup_sqlite,str(self.db.engine.url),self.provider.root,destination)
        target=self.root/'issuer-restore';await restore_sqlite(destination,target)
        db=Database('sqlite+aiosqlite:///'+str(target/'state.sqlite'))
        try:
            async with db.read(self.scope) as c:
                after=dict((await c.execute(select(issuers).where(issuers.c.delegation_ref==child['id']))).mappings().one())
            self.assertEqual(before,after)
        finally:await db.close()

    async def test_identity_permission_and_deadline_are_authority(self):
        self.directory["principals"]["issuer"]["expires_at"] = time.time() + 30
        self.save_identity()
        response = await self.http.post(
            "/api/os/runtime/delegations", headers=self.headers(), json=self.body
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.directory["principals"]["issuer"]["expires_at"] = time.time() + 1200
        self.directory["principals"]["issuer"]["profiles"]["test-profile"][
            "actions"
        ] = []
        self.save_identity()
        self.assertEqual(
            (
                await self.http.post(
                    "/api/os/runtime/delegations",
                    headers=self.headers(),
                    json=self.body,
                )
            ).status_code,
            403,
        )
