"""Qualify independent source sessions against the synthetic development IdP and real MCP."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from authlib.integrations.httpx_client import OAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.jws import JWSRegistry

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]


def main():
    import test_runtime_oauth_source as fixtures
    from app.runtime.database import RuntimeErrorCode
    from app.runtime.mcp_tool_transport import McpHttpTransport
    from app.runtime.principals import FilePrincipals
    from app.runtime.work_authorizations import view

    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-directory", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    fixture = json.loads((Path(args.fixture_directory) / "fixture.json").read_text())
    info = json.loads(
        subprocess.check_output(["docker", "inspect", fixture["container"]], text=True)
    )[0]
    assert info["Config"]["Labels"].get("dios.purpose") == "development-identity"
    issuer = "http://127.0.0.1:13187/realms/dios-development"
    redirect = "http://127.0.0.1:17654/callback"
    checks = []
    minted = []

    def check(name, value=True):
        assert value, name
        checks.append(name)

    def private(path, value):
        path.write_text(value)
        path.chmod(0o600)

    with (
        tempfile.TemporaryDirectory(prefix="dios-source-sessions-") as temporary,
        httpx.Client(trust_env=False, timeout=5) as http,
    ):
        root = Path(temporary)
        metadata = http.get(issuer + "/.well-known/openid-configuration").json()
        client_file = root / "client-secret"
        private(client_file, fixture["secret"])
        config = {
            "issuer": issuer,
            "audience": "dios-api",
            "endpoint": metadata["introspection_endpoint"],
            "client_id": "dios-api",
            "client_secret_file": str(client_file),
            "timeout_seconds": 2,
        }

        def claims(token):
            with OAuth2Client(
                "dios-api", fixture["secret"], trust_env=False, timeout=5
            ) as client:
                response = client.introspect_token(
                    metadata["introspection_endpoint"], token=token
                )
                response.raise_for_status()
                return response.json()

        def reference(name, token, ref, version):
            data = claims(token["access_token"])
            assert data["active"] is True
            path = root / name
            private(path, token["access_token"])
            return {
                "ref": ref,
                "version": version,
                "path": str(path),
                "sha256": hashlib.sha256(token["access_token"].encode()).hexdigest(),
                "issuer": issuer,
                "subject": data["sub"],
                "audience": "dios-api",
                "client_id": "portal",
                "expires_at": data["exp"],
            }

        def login():
            verifier, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
            with OAuth2Client(
                "portal",
                token_endpoint_auth_method="none",
                redirect_uri=redirect,
                scope="openid",
                code_challenge_method="S256",
                trust_env=False,
                timeout=10,
            ) as client:
                url, state = client.create_authorization_url(
                    metadata["authorization_endpoint"],
                    code_verifier=verifier,
                    nonce=nonce,
                )
                result = subprocess.run(
                    [args.node, str(ROOT / "scripts/probe_identity_login.mjs")],
                    input=json.dumps(
                        {
                            "url": url,
                            "username": "source-user",
                            "password": fixture["password"],
                            "redirect": redirect,
                        }
                    ),
                    capture_output=True,
                    text=True,
                    timeout=40,
                    check=False,
                )
                assert result.returncode == 0, "Browser login failed"
                token = client.fetch_token(
                    metadata["token_endpoint"],
                    authorization_response=json.loads(result.stdout)["callback"],
                    state=state,
                    code_verifier=verifier,
                )
            decoded = jwt.decode(
                token["id_token"],
                key=KeySet.import_key_set(http.get(metadata["jwks_uri"]).json()),
                registry=JWSRegistry(algorithms=["RS256"]),
            )
            CodeIDToken(
                decoded.claims,
                decoded.header,
                {"iss": {"essential": True, "value": issuer}},
                {
                    "nonce": nonce,
                    "client_id": "portal",
                    "access_token": token["access_token"],
                },
            ).validate(leeway=0)
            minted.append(token)
            return token

        def revoke(token):
            with OAuth2Client(
                "portal", token_endpoint_auth_method="none", trust_env=False, timeout=5
            ) as client:
                response = client.revoke_token(
                    metadata["revocation_endpoint"],
                    token=token["access_token"],
                    token_type_hint="access_token",
                )
                response.raise_for_status()

        first, peer = login(), login()
        first_claims, peer_claims = (
            claims(first["access_token"]),
            claims(peer["access_token"]),
        )
        check(
            "real-browser-same-subject-independent-IdP-sessions",
            first_claims["sub"] == peer_claims["sub"]
            and first_claims.get("sid")
            and peer_claims.get("sid")
            and first_claims["sid"] != peer_claims["sid"],
        )
        source_a = reference("source-a-v1", first, "qualification/session-a", "v1")
        source_b = reference("source-b-v1", peer, "qualification/session-b", "v1")

        class Integration(unittest.IsolatedAsyncioTestCase):
            def configure(self, _):
                self.config, self.reference = config, source_a
                self.expiry = source_a["expires_at"]
                self.access = first["access_token"]
                self.transport = None

            async def asyncSetUp(self):
                await fixtures.OAuthToolTests.asyncSetUp(self)
                self.directory = json.loads(self.identity_file.read_text())
                self.directory["oauth_sources"] = {"a-v1": source_a, "b-v1": source_b}
                row = self.directory["principals"]["a"]
                del row["upstream"]
                row["source_refs"] = ["a-v1", "b-v1"]
                self.save()
                self.calls = []
                owner = self

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass

                    def do_POST(self):
                        request = json.loads(
                            self.rfile.read(int(self.headers["Content-Length"]))
                        )
                        if request["method"] == "notifications/initialized":
                            self.send_response(202)
                            self.end_headers()
                            return
                        if request["method"] == "initialize":
                            result = {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {"tools": {}},
                                "serverInfo": {
                                    "name": "source-sessions",
                                    "version": "1",
                                },
                            }
                        else:
                            owner.calls.append(request)
                            result = {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Independent source evidence",
                                    }
                                ],
                                "isError": False,
                            }
                        raw = json.dumps(
                            {"jsonrpc": "2.0", "id": request["id"], "result": result}
                        ).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)

                self.server = ThreadingHTTPServer(("127.0.0.1", 19999), Handler)
                self.thread = threading.Thread(
                    target=self.server.serve_forever, daemon=True
                )
                self.thread.start()
                self.gate.transport = McpHttpTransport()

            def save(self):
                private(self.identity_file, json.dumps(self.directory))

            async def asyncTearDown(self):
                await asyncio.to_thread(self.server.shutdown)
                self.server.server_close()
                self.thread.join(2)
                await fixtures.OAuthToolTests.asyncTearDown(self)

            async def test_independent_sources_at_execution_boundary(self):
                original = await view(self.store, self.run["id"])
                old_input = (await self.store.get(self.run["id"]))["input"]
                item = await fixtures.OAuthToolTests.approved(self)

                def refresh():
                    with OAuth2Client(
                        "portal",
                        token_endpoint_auth_method="none",
                        trust_env=False,
                        timeout=5,
                    ) as client:
                        value = client.refresh_token(
                            metadata["token_endpoint"],
                            refresh_token=first["refresh_token"],
                        )
                        minted.append(value)
                        return reference("source-a-v2", value, source_a["ref"], "v2")

                refreshed = await asyncio.to_thread(refresh)
                self.directory["oauth_sources"]["a-v2"] = refreshed
                self.directory["principals"]["a"]["source_refs"].append("a-v2")
                self.save()
                await self.verifier(self.context, "execute")
                check(
                    "native-refresh-new-version-preserves-original-work",
                    (await self.store.get(self.run["id"]))["input"] == old_input
                    and (await view(self.store, self.run["id"]))["expires_at"]
                    == original["expires_at"],
                )
                result = await self.gate.dispatch(
                    self.run["id"], item["id"], item["version"]
                )
                check(
                    "original-approved-tool-executes-after-refresh",
                    result["state"] == "succeeded" and len(self.calls) == 1,
                )
                identity = await asyncio.to_thread(
                    FilePrincipals(self.identity_file).authenticate,
                    peer["access_token"],
                    self.scope,
                )
                descriptor = await asyncio.to_thread(
                    self.policy.admission,
                    "test-profile",
                    self.spec.fingerprint(),
                    [],
                    principal=identity,
                )
                peer_run = await self.store.submit(
                    "peer-source-run",
                    {
                        "runtime_profile": "test-profile",
                        "profile_digest": self.spec.fingerprint(),
                        "secret_refs": [],
                        "principal": identity,
                    },
                    authorization=descriptor,
                )
                await self.store.register_worker(
                    "peer-worker", "peer-instance", "host", os.getpid()
                )
                attempt = await self.store.claim("peer-worker", "peer-instance")
                assert attempt["run_id"] == peer_run["id"]
                binding = (
                    peer_run["id"],
                    attempt["id"],
                    "peer-worker",
                    "peer-instance",
                    attempt["epoch"],
                )
                context = replace(
                    self.context,
                    run_id=peer_run["id"],
                    attempt_id=attempt["id"],
                    worker_id="peer-worker",
                    worker_instance="peer-instance",
                    lease_epoch=attempt["epoch"],
                    authorization_epoch=attempt["authorization_epoch"],
                    session_id=None,
                    authorization_ref=peer_run["input"]["work_authorization_ref"],
                )
                await self.verifier(context, "execute")
                await self.store.report(*binding, 1, "preparing")
                await self.store.report(*binding, 2, "creating_session")
                context = replace(context, session_id="peer-session")
                await self.verifier(context, "execute")
                await self.store.report(
                    *binding, 3, "waiting_model", session_id="peer-session"
                )
                peer_tool = await self.gate.request(
                    peer_run["id"],
                    attempt["id"],
                    "peer-session",
                    "search",
                    {"query": "peer"},
                    "peer-call",
                )
                peer_tool = await self.gate.decide(
                    peer_run["id"],
                    peer_tool["id"],
                    peer_tool["version"],
                    "approved",
                    "fixture-approval",
                )
                denied = await self.gate.request(
                    self.run["id"],
                    self.attempt["id"],
                    "session-one",
                    "search",
                    {"query": "revoked"},
                    "revoked-call",
                )
                denied = await self.gate.decide(
                    self.run["id"],
                    denied["id"],
                    denied["version"],
                    "approved",
                    "fixture-approval",
                )
                await asyncio.to_thread(revoke, first)
                check(
                    "source-session-revoked-peer-remains-active",
                    not (await asyncio.to_thread(claims, first["access_token"]))[
                        "active"
                    ]
                    and (await asyncio.to_thread(claims, peer["access_token"]))[
                        "active"
                    ],
                )
                with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
                    await self.gate.dispatch(
                        self.run["id"], denied["id"], denied["version"]
                    )
                check(
                    "revoked-session-tool-blocked-before-MCP-call", len(self.calls) == 1
                )
                result = await self.gate.dispatch(
                    peer_run["id"], peer_tool["id"], peer_tool["version"]
                )
                check(
                    "independent-peer-approved-tool-still-executes",
                    result["state"] == "succeeded" and len(self.calls) == 2,
                )
                with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"):
                    await self.gate.dispatch(
                        self.run["id"], denied["id"], denied["version"]
                    )
                check(
                    "revocation-remains-persistent",
                    (await view(self.store, self.run["id"]))["state"] == "revoked",
                )
                text = json.dumps(await self.store.get(self.run["id"])) + json.dumps(
                    await self.store.get(peer_run["id"])
                )
                check(
                    "credentials-absent-from-persisted-runs",
                    all(t["access_token"] not in text for t in minted),
                )

        try:
            result = unittest.TextTestRunner(verbosity=2).run(
                unittest.defaultTestLoader.loadTestsFromTestCase(Integration)
            )
            if not result.wasSuccessful():
                raise SystemExit(1)
            Path(args.output).write_text(
                json.dumps(
                    {
                        "recorded_at_utc": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                        "checks": checks,
                        "checks_passed": len(checks),
                        "actual_tools_calls": 2,
                        "scope": "Actual synthetic IdP browser sessions, native OAuth refresh/revocation, DiOS E2AG and real MCP HTTP; isolated Runtime database",
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            for token in minted:
                try:
                    revoke(token)
                except httpx.HTTPError:
                    pass


if __name__ == "__main__":
    main()
