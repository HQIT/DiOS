"""Opt-in native Keycloak delivery. Requires a dedicated synthetic realm fixture."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx
import uvicorn
import test_runtime_oauth_sessions as sessions
import test_runtime_oauth_source as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.logout_schema import events
from app.runtime.work_authorizations import view
from sqlalchemy import select, func


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_LOGOUT_FIXTURE"),
    "native Keycloak logout fixture not supplied",
)
class NativeLogoutTests(fixtures.SourceFixture, unittest.IsolatedAsyncioTestCase):
    save = sessions.MultiSourceTests.save
    add_source = sessions.MultiSourceTests.add_source
    submit_as = sessions.MultiSourceTests.submit_as
    headers_for = sessions.MultiSourceTests.headers_for
    context = sessions.MultiSourceTests.context
    get = sessions.MultiSourceTests.get

    async def asyncSetUp(self):
        await sessions.MultiSourceTests.asyncSetUp(self)
        self.override.stop()
        fixture = json.loads(Path(os.environ["DIOS_TEST_LOGOUT_FIXTURE"]).read_text())
        self.native = fixture
        self.native_tokens = {}
        issuer = fixture["issuer"]
        self.secret_file.write_text(fixture["client_secret"])
        self.config.update(
            issuer=issuer,
            endpoint=issuer + "/protocol/openid-connect/token/introspect",
            timeout_seconds=2,
        )
        self.directory["oauth2"] = self.config
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            for name, ref in [("a", "session-a-v1"), ("peer", "peer")]:
                response = await client.post(
                    issuer + "/protocol/openid-connect/token",
                    data={
                        "grant_type": "password",
                        "client_id": "portal",
                        "username": "synthetic-user",
                        "password": fixture["password"],
                        "scope": "openid",
                    },
                )
                response.raise_for_status()
                token = response.json()
                self.native_tokens[name] = token
                response = await client.post(
                    self.config["endpoint"],
                    auth=("dios-api", fixture["client_secret"]),
                    data={"token": token["access_token"]},
                )
                response.raise_for_status()
                claims = response.json()
                self.assertTrue(claims["active"])
                source = self.directory["oauth_sources"][ref]
                Path(source["path"]).write_text(token["access_token"])
                self.tokens[name] = token["access_token"]
                source.update(
                    issuer=issuer,
                    subject=claims["sub"],
                    expires_at=claims["exp"],
                    sha256=hashlib.sha256(token["access_token"].encode()).hexdigest(),
                    session_id=claims["sid"],
                )
                if "auth_time" in claims:
                    source["session_started_at"] = claims["auth_time"]
        self.save()
        config = self.root / "logout.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        "native": {
                            "issuer": issuer,
                            "client_id": "portal",
                            "jwks_uri": issuer + "/protocol/openid-connect/certs",
                            "algorithms": ["RS256"],
                        }
                    },
                }
            )
        )
        self.environment = patch.dict(
            os.environ,
            {
                "DIOS_RUNTIME_LOGOUT_CONFIG": str(config),
                "DIOS_RUNTIME_LOGOUT_VERIFIER": fixture["verifier"],
            },
        )
        self.environment.start()
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=13202,
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.server.started)

    async def asyncTearDown(self):
        self.server.should_exit = True
        await self.server_task
        self.environment.stop()
        await fixtures.OAuthPrincipalTests.asyncTearDown(self)

    async def test_native_push_revokes_only_logged_out_session_without_polling(self):
        a = await self.submit_as("a", "native-a")
        context = await self.context(a)
        await self.verifier(context, "execute")
        b = await self.submit_as("peer", "native-b")
        self.assertNotEqual(
            self.directory["oauth_sources"]["session-a-v1"]["session_id"],
            self.directory["oauth_sources"]["peer"]["session_id"],
        )
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            invalid = await client.post(
                "http://127.0.0.1:13202/api/os/runtime/identity/logout/native",
                data={"logout_token": "unsigned"},
            )
            self.assertEqual(invalid.status_code, 400, invalid.text)
            async with self.db.read("one") as c:
                self.assertEqual(
                    await c.scalar(select(func.count()).select_from(events)), 0
                )
            response = await client.post(
                self.native["issuer"] + "/protocol/openid-connect/logout",
                data={
                    "client_id": "portal",
                    "refresh_token": self.native_tokens["a"]["refresh_token"],
                },
            )
            self.assertEqual(response.status_code, 204, response.text)
        # No scheduler or source reconciler runs in this test; only the IdP POST can write this receipt.
        for _ in range(50):
            detail = await view(self.console.store, a["id"])
            if detail["source_event"]:
                break
            await asyncio.sleep(0.1)
        self.assertIsNotNone(detail["source_event"])
        self.assertEqual(detail["state"], "revoked")
        self.assertEqual(detail["grant"]["state"], "revoked")
        self.assertEqual((await view(self.console.store, b["id"]))["state"], "active")
        self.assertEqual((await self.get("peer", "identity")).status_code, 200)
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
            await self.verifier(context, "execute")
        Path(self.native["evidence"]).write_text(
            json.dumps(
                {
                    "native_delivery": True,
                    "invalid_token_no_event": True,
                    "polling_disabled": True,
                    "matching_work_and_grant_revoked": True,
                    "independent_session_preserved": True,
                    "source_event": detail["source_event"],
                    "live_process_test": False,
                },
                indent=2,
            )
        )
