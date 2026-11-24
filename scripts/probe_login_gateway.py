"""Native browser gateway qualification with an isolated IdP and Valkey store."""

import argparse
import base64
import hashlib
import json
import os
import secrets
import selectors
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from authlib.integrations.httpx_client import OAuth2Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
PROXY = "quay.io/oauth2-proxy/oauth2-proxy:v7.15.6@sha256:c3415e8660af553f5e18f5d90db050614c02493c0ac6260ac59442f6d9b12048"
IDP = "quay.io/keycloak/keycloak:26.8.2@sha256:7e66a96e4739239070aeaf72e4c5decf0f7f682cfc725b9f7ea2b336eaba38b3"
STORE = "valkey/valkey:9.2.0@sha256:1c2b25b09f354c66fe994733acdf12fcfd4bdf6f3bd92b88a93a70378d884056"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    checks = []
    observations = {}
    containers = []
    browser = None
    server = None
    tokens = {}
    count = [0]
    for port in (13192, 13194, 13195, 13196):
        with socket.socket() as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(("127.0.0.1", port))
    base = "http://127.0.0.1:13194"
    issuer = "http://127.0.0.1:13192/realms/gateway-qualification"
    api_secret = secrets.token_urlsafe(32)
    password = secrets.token_urlsafe(32)

    def check(name, value=True):
        assert value, name
        checks.append(name)
        print(name, flush=True)

    def docker(*args):
        return subprocess.check_output(
            ["docker", *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def private(path, text):
        path.write_text(text)
        path.chmod(0o600)

    with (
        tempfile.TemporaryDirectory(prefix="dios-login-gateway-") as tmp,
        httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as http,
    ):
        d = Path(tmp)

        def ready(url):
            for _ in range(180):
                try:
                    response = http.get(url)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            raise AssertionError("service readiness timeout")

        def introspect(token):
            with OAuth2Client("dios-api", api_secret, trust_env=False, timeout=2) as c:
                response = c.introspect_token(
                    issuer + "/protocol/openid-connect/token/introspect", token=token
                )
                response.raise_for_status()
                return response.json()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                count[0] += 1
                token = self.headers.get("X-Forwarded-Access-Token", "")
                digest = hashlib.sha256(token.encode()).hexdigest()
                tokens[digest] = token
                try:
                    claims = introspect(token)
                except httpx.HTTPError:
                    claims = {"active": None}
                value = {
                    "active": claims.get("active"),
                    "subject": claims.get("sub"),
                    "audience": claims.get("aud"),
                    "client": claims.get("client_id"),
                    "expires_at": claims.get("exp"),
                    "token_digest": digest,
                    "forwarded_user": self.headers.get("X-Forwarded-User"),
                    "authorization_is_forged": self.headers.get("Authorization")
                    == "Bearer forged",
                    "xauth_is_forged": self.headers.get("X-Auth-Request-User")
                    == "forged",
                    "origin": self.headers.get("Origin"),
                }
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_POST = do_GET

        def runtime_source_check(result):
            from app.runtime.database import RuntimeErrorCode
            from app.runtime.oauth_source import OAuthSource

            data = result["data"]
            private(d / "runtime-source", tokens[data["token_digest"]])
            private(d / "runtime-client", api_secret)
            provider = {
                "issuer": issuer,
                "audience": "dios-api",
                "endpoint": issuer + "/protocol/openid-connect/token/introspect",
                "client_id": "dios-api",
                "client_secret_file": str(d / "runtime-client"),
                "timeout_seconds": 2,
            }
            reference = {
                "ref": "qualification/source",
                "version": "v1",
                "path": str(d / "runtime-source"),
                "sha256": data["token_digest"],
                "issuer": issuer,
                "subject": data["subject"],
                "audience": "dios-api",
                "client_id": "portal",
                "expires_at": data["expires_at"],
            }
            try:
                OAuthSource(provider).current(reference)
                return "active"
            except RuntimeErrorCode as exc:
                return exc.code

        def send(**message):
            browser.stdin.write(json.dumps(message) + "\n")
            browser.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(browser.stdout, selectors.EVENT_READ)
                if not selector.select(90):
                    raise TimeoutError("Browser helper response timed out")
            line = browser.stdout.readline()
            assert line, "Browser helper exited"
            value = json.loads(line)
            assert value["ok"], value
            return value["result"]

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 13196), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            realm = {
                "realm": "gateway-qualification",
                "enabled": True,
                "sslRequired": "none",
                "accessTokenLifespan": 30,
                "ssoSessionIdleTimeout": 300,
                "ssoSessionMaxLifespan": 600,
                "revokeRefreshToken": True,
                "refreshTokenMaxReuse": 0,
                "users": [
                    {
                        "username": u,
                        "enabled": True,
                        "email": u + "@example.invalid",
                        "emailVerified": True,
                        "firstName": u,
                        "lastName": "Qualification",
                        "credentials": [
                            {"type": "password", "value": password, "temporary": False}
                        ],
                    }
                    for u in ("alice", "bob")
                ],
                "clients": [
                    {
                        "clientId": "portal",
                        "publicClient": False,
                        "secret": api_secret,
                        "standardFlowEnabled": True,
                        "redirectUris": [base + "/oauth2/callback"],
                        "attributes": {"pkce.code.challenge.method": "S256"},
                        "protocolMappers": [
                            {
                                "name": "aud-api",
                                "protocol": "openid-connect",
                                "protocolMapper": "oidc-audience-mapper",
                                "config": {
                                    "included.client.audience": "dios-api",
                                    "access.token.claim": "true",
                                    "id.token.claim": "false",
                                    "introspection.token.claim": "true",
                                },
                            }
                        ],
                    },
                    {
                        "clientId": "dios-api",
                        "publicClient": False,
                        "secret": api_secret,
                        "standardFlowEnabled": False,
                    },
                ],
            }
            imp = d / "import"
            imp.mkdir()
            private(imp / "realm.json", json.dumps(realm))
            (d / "store").mkdir()
            idp = docker(
                "run",
                "-d",
                "--network",
                "host",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--memory",
                "1g",
                "--cpus",
                "2",
                "--label",
                "dios.purpose=gateway-qualification",
                "-v",
                str(imp) + ":/opt/keycloak/data/import:ro",
                IDP,
                "start-dev",
                "--import-realm",
                "--http-host",
                "127.0.0.1",
                "--http-port",
                "13192",
                "--hostname",
                "http://127.0.0.1:13192",
            )
            containers.append(idp)
            store_secret = secrets.token_urlsafe(32)
            private(
                d / "store.conf",
                f"bind 127.0.0.1\nport 13195\ndir /data\nappendonly yes\nrequirepass {store_secret}\n",
            )
            store = docker(
                "run",
                "-d",
                "--network",
                "host",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--memory",
                "256m",
                "--label",
                "dios.purpose=gateway-qualification",
                "-v",
                str(d / "store") + ":/data",
                "-v",
                str(d / "store.conf") + ":/fixture.conf:ro",
                STORE,
                "valkey-server",
                "/fixture.conf",
            )
            containers.append(store)
            ready(issuer + "/.well-known/openid-configuration")
            config = {
                "http_address": "127.0.0.1:13194",
                "provider": "oidc",
                "oidc_issuer_url": issuer,
                "client_id": "portal",
                "client_secret": api_secret,
                "redirect_url": base + "/oauth2/callback",
                "scope": "openid email profile",
                "code_challenge_method": "S256",
                "insecure_oidc_skip_nonce": False,
                "cookie_secret": base64.urlsafe_b64encode(
                    secrets.token_bytes(32)
                ).decode(),
                "cookie_secure": False,
                "cookie_httponly": True,
                "cookie_samesite": "lax",
                "cookie_refresh": "5s",
                "cookie_expire": "5m",
                "session_store_type": "redis",
                "redis_connection_url": f"redis://:{store_secret}@127.0.0.1:13195/0",
                "email_domains": ["example.invalid"],
                "upstreams": ["http://127.0.0.1:13196/"],
                "skip_provider_button": True,
                "pass_access_token": True,
                "pass_user_headers": True,
                "pass_basic_auth": False,
                "pass_authorization_header": False,
                "auth_logging": False,
                "request_logging": False,
                "standard_logging": False,
            }

            def save_config():
                private(
                    d / "proxy.cfg",
                    "\n".join(k + " = " + json.dumps(v) for k, v in config.items()),
                )

            save_config()
            proxy = docker(
                "run",
                "-d",
                "--network",
                "host",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--memory",
                "256m",
                "--label",
                "dios.purpose=gateway-qualification",
                "-v",
                str(d / "proxy.cfg") + ":/proxy.cfg:ro",
                PROXY,
                "--config=/proxy.cfg",
            )
            containers.append(proxy)
            ready(base + "/ready")
            check(
                "anonymous-request-denied",
                http.get(base + "/who").status_code != 200 and count[0] == 0,
            )
            err = (d / "browser.stderr").open("w")
            browser = subprocess.Popen(
                [args.node, str(ROOT / "scripts/probe_login_gateway_browser.mjs")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                bufsize=1,
            )
            send(action="init", base=base, issuer=issuer, password=password)
            first = send(action="login", name="alice", username="alice")
            check(
                "real-browser-code-PKCE-nonce-state",
                first["status"] == 200 and all(first["authorize"].values()),
            )
            check(
                "audience-client-and-subject",
                first["data"]["active"] is True
                and "dios-api" in first["data"]["audience"]
                and first["data"]["client"] == "portal"
                and first["data"]["subject"] == first["data"]["forwarded_user"],
            )
            check(
                "http-only-lax-ticket-no-browser-token-storage",
                bool(first["cookies"])
                and all(
                    x["httpOnly"] and x["sameSite"] == "Lax" and x["length"] < 500
                    for x in first["cookies"]
                )
                and first["storage"] == {"local": [], "session": []},
            )
            check(
                "authorization-callback-replay-rejected",
                send(action="replay", name="alice")["status"] >= 400,
            )
            check(
                "tampered-ticket-rejected",
                send(action="tamper", name="alice")["status"] != 200,
            )
            for bad in ("state", "nonce", "pkce"):
                before = count[0]
                result = send(action="login", name=bad, username="alice", bad=bad)
                check(
                    bad + "-mismatch-rejected",
                    result["status"] != 200 and count[0] == before,
                )
            spoof = send(
                action="get",
                name="alice",
                options={
                    "headers": {
                        "X-Forwarded-User": "forged",
                        "X-Forwarded-Email": "forged",
                        "X-Forwarded-Access-Token": "forged",
                        "Authorization": "Bearer forged",
                        "X-Auth-Request-User": "forged",
                    }
                },
            )
            check(
                "managed-user-and-access-token-headers-overwritten",
                spoof["data"]["subject"] == first["data"]["subject"]
                and spoof["data"]["forwarded_user"] != "forged"
                and spoof["data"]["active"] is True,
            )
            observations["unused_headers_forwarded"] = {
                k: spoof["data"][k]
                for k in ("authorization_is_forged", "xauth_is_forged")
            }
            csrf = send(
                action="get",
                name="alice",
                path="/action",
                options={
                    "method": "POST",
                    "headers": {"Origin": "https://untrusted.example"},
                },
            )
            observations["upstream_post_with_untrusted_origin_status"] = csrf["status"]
            original_exp = first["data"]["expires_at"]
            last = first
            rounds = 0
            while time.time() <= original_exp + 1:
                time.sleep(6)
                batch = send(action="parallel", name="alice")
                assert len(batch) == 6 and all(
                    x["status"] == 200 and x["data"]["active"] is True for x in batch
                ), "parallel refresh"
                rounds += 1
                last = batch[-1]
            observations["parallel_refresh"] = {
                "rounds": rounds,
                "requests_per_round": 6,
            }
            check("parallel-refresh-beyond-original-expiry", rounds > 0)
            check(
                "native-refresh-survives-original-access-expiry",
                last["data"]["expires_at"] > original_exp
                and last["data"]["token_digest"] != first["data"]["token_digest"],
            )
            bob = send(action="login", name="bob", username="bob")
            check(
                "independent-user-session",
                bob["data"]["subject"] != first["data"]["subject"],
            )
            docker("restart", proxy)
            ready(base + "/ready")
            check(
                "gateway-restart-preserves-session",
                send(action="get", name="alice")["status"] == 200,
            )
            docker("restart", store)
            ready(base + "/ready")
            check(
                "AOF-store-restart-preserves-session",
                send(action="get", name="alice")["status"] == 200,
            )
            out = send(action="logout", name="alice")
            check("local-logout-denies-session", out["after"]["status"] != 200)
            check(
                "logged-out-ticket-replay-rejected",
                send(action="replay-cookie", name="alice")["status"] != 200,
            )
            check(
                "local-logout-keeps-IdP-SSO",
                send(action="sso-login", name="alice")["status"] == 200,
            )
            config["backend_logout_url"] = (
                issuer + "/protocol/openid-connect/logout?id_token_hint={id_token}"
            )
            save_config()
            docker("restart", proxy)
            ready(base + "/ready")
            before = send(action="get", name="alice")
            token = tokens[before["data"]["token_digest"]]
            send(action="logout", name="alice")
            check(
                "configured-backend-logout-invalidates-IdP-source",
                introspect(token).get("active") is False,
            )
            check(
                "other-user-survives-independent-logout",
                send(action="get", name="bob")["status"] == 200,
            )
            docker("stop", store)
            response = send(action="get", name="bob")
            check("store-outage-fails-closed", response["status"] != 200)
            docker("start", store)
            ready(base + "/ready")
            observations["after_store_outage_status"] = send(action="get", name="bob")[
                "status"
            ]
            offline = send(action="login", name="offline", username="bob")
            check(
                "DiOS-source-accepts-current-gateway-token",
                runtime_source_check(offline) == "active",
            )
            time.sleep(6)
            docker("stop", "--time", "1", idp)

            def observed_response(result):
                data = result.get("data") or {}
                return {
                    "status": result["status"],
                    "source_active": data.get("active"),
                    "source_expires_at": data.get("expires_at"),
                    "observed_at": int(time.time()),
                    "upstream_reached": bool(data),
                    "same_token": data.get("token_digest")
                    == offline["data"]["token_digest"],
                }

            observations["DiOS_source_during_IdP_outage"] = runtime_source_check(
                offline
            )
            check(
                "DiOS-source-fails-closed-during-IdP-outage",
                observations["DiOS_source_during_IdP_outage"]
                == "AUTH_PROVIDER_UNAVAILABLE",
            )
            observations["IdP_outage_refresh"] = observed_response(
                send(action="get", name="offline")
            )
            time.sleep(max(0, offline["data"]["expires_at"] - time.time() + 2))
            observations["IdP_outage_after_source_expiry"] = observed_response(
                send(action="get", name="offline")
            )
            check(
                "DiOS-source-rejects-expired-token",
                runtime_source_check(offline) == "AUTH_PRINCIPAL_EXPIRED",
            )
            docker("start", idp)
            ready(issuer + "/.well-known/openid-configuration")
            observations["after_IdP_recovery"] = observed_response(
                send(action="get", name="offline")
            )
            recovered = send(action="login", name="recovered", username="bob")
            check(
                "fresh-login-after-dependency-recovery",
                recovered["status"] == 200 and recovered["data"]["active"] is True,
            )
            evidence = {
                "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "images": {
                    "gateway": PROXY,
                    "session_store": STORE,
                    "identity_provider": IDP,
                },
                "checks": checks,
                "checks_passed": len(checks),
                "observations": observations,
                "scope": "Native gateway and existing DiOS OAuth source validator; no browser principal intake or runtime migration",
                "remaining": [
                    "Production TLS and Secure cookies",
                    "Gateway-only backend transport and application CSRF protection",
                    "Versioned source-session intake and concurrent browser sessions",
                    "Background work source lifetime and OAuth refresh semantics",
                    "Standard logout/security events and recovery reconciliation",
                ],
            }
            Path(args.output).write_text(json.dumps(evidence, indent=2) + "\n")
        except BaseException:
            for i, cid in enumerate(containers):
                logs = subprocess.run(
                    ["docker", "logs", cid], capture_output=True, text=True, check=False
                )
                private(
                    Path(args.output + f".diagnostic-{i}"), logs.stdout + logs.stderr
                )
            raise
        finally:
            if browser:
                browser.stdin.close()
                try:
                    browser.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    browser.terminate()
                    browser.wait(timeout=10)
            if server:
                server.shutdown()
                server.server_close()
            for cid in reversed(containers):
                subprocess.run(
                    ["docker", "rm", "-f", cid],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )


if __name__ == "__main__":
    main()
