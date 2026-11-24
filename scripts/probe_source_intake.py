"""Native create-only OpenBao admission with a dedicated revocable OAuth source."""

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]


def main():
    import test_runtime_oauth_source as fixtures
    from app.runtime.database import RuntimeErrorCode
    from app.runtime.openbao_secrets import OpenBaoSecrets
    from app.runtime.principals import FilePrincipals
    from app.runtime.work_authorizations import view

    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", required=True)
    parser.add_argument("--secret-fixture-directory", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    external = json.loads(Path(args.source_config).read_text())
    d = Path(args.secret_fixture_directory)
    server = json.loads((d / "containers.json").read_text())["server"]
    for cid, label in [
        (server, "development-secrets"),
        (external["container"], "development-identity"),
    ]:
        info = json.loads(
            subprocess.check_output(["docker", "inspect", cid], text=True)
        )[0]
        assert info["Config"]["Labels"].get("dios.purpose") == label
    assert (
        external["provider"]["issuer"]
        == "http://127.0.0.1:13187/realms/dios-development"
    )
    assert external["reference"]["expires_at"] > time.time() + 120
    base = "http://127.0.0.1:13188"
    root_token = json.loads((d / "fixture.json").read_text())["root_token"]
    mount = "intake-" + uuid.uuid4().hex
    policies = [mount + "-reader", mount + "-writer"]
    checks = []
    issued = []

    def admin(method, path, body=None):
        with httpx.Client(trust_env=False, timeout=5) as http:
            result = http.request(
                method,
                base + "/v1/" + path,
                headers={"X-Vault-Token": root_token},
                json=body,
            )
            assert result.is_success, (path, result.status_code)
            return result.json() if result.content else {}

    def check(name, value=True):
        assert value, name
        checks.append(name)

    def private(path, value):
        path.write_text(value)
        path.chmod(0o600)

    def role_request(token, method, path, body=None):
        with httpx.Client(trust_env=False, timeout=5) as http:
            return http.request(
                method,
                base + "/v1/" + path,
                headers={"X-Vault-Token": token},
                json=body,
            )

    with tempfile.TemporaryDirectory(prefix="dios-intake-") as tmp:
        root = Path(tmp)

        def issue(policy, path):
            token = admin(
                "POST",
                "auth/token/create",
                {"policies": [policy], "no_default_policy": True, "ttl": "20m"},
            )["auth"]["client_token"]
            issued.append(token)
            private(path, token)
            return token

        try:
            admin(
                "POST",
                "sys/mounts/" + mount,
                {"type": "kv", "options": {"version": "2"}},
            )
            for suffix, permissions in [
                ("reader", ["read"]),
                ("writer", ["create", "read"]),
            ]:
                admin(
                    "PUT",
                    "sys/policies/acl/" + mount + "-" + suffix,
                    {
                        "policy": f'path "{mount}/data/admission/*" {{ capabilities = {json.dumps(permissions)} }}'
                    },
                )
            reader = issue(policies[0], root / "reader")
            writer = issue(policies[1], root / "writer")
            store = {
                "address": base,
                "token_file": str(root / "reader"),
                "timeout_seconds": 2,
            }
            provider = dict(external["provider"], secret_store=store)
            intake = {
                "writer": dict(store, token_file=str(root / "writer")),
                "mount": mount,
                "prefix": "admission",
                "timeout_seconds": 5,
                "max_source_ttl_seconds": 3600,
            }
            access = Path(external["reference"]["path"]).read_text()

            class NativeIntake(unittest.IsolatedAsyncioTestCase):
                def configure(self, _):
                    self.config = provider
                    self.reference = external["reference"]
                    self.expiry = self.reference["expires_at"]
                    self.access = access
                    self.transport = None

                async def asyncSetUp(self):
                    await fixtures.OAuthPrincipalTests.asyncSetUp(self)
                    row = self.directory["principals"]["a"]
                    del row["upstream"]
                    row["source_refs"] = []
                    row["oauth_identity"] = {
                        k: self.reference[k] for k in ("issuer", "subject", "client_id")
                    }
                    row["oauth_identity"]["required_scopes"] = ["openid"]
                    self.directory["oauth_intake"] = intake
                    self.save()

                asyncTearDown = fixtures.OAuthPrincipalTests.asyncTearDown
                save = fixtures.OAuthPrincipalTests.save
                headers_for = fixtures.OAuthPrincipalTests.headers_for
                submit_as = fixtures.OAuthPrincipalTests.submit_as
                get = fixtures.OAuthPrincipalTests.get
                context = fixtures.OAuthPrincipalTests.context

                async def authenticate(self):
                    return await asyncio.to_thread(
                        FilePrincipals(self.identities).authenticate, access, "one"
                    )

                async def test_native_admission_and_recovery(self):
                    catalog = self.identities.read_bytes()
                    bindings = await asyncio.gather(
                        *(self.authenticate() for _ in range(6))
                    )
                    check(
                        "six-native-concurrent-admissions-one-binding",
                        all(x == bindings[0] for x in bindings),
                    )
                    key = bindings[0]["upstream"]["ref"].split("/")[1]
                    meta = await asyncio.to_thread(
                        admin, "GET", f"{mount}/metadata/admission/{key}"
                    )
                    check(
                        "native-CAS-one-version", meta["data"]["current_version"] == 1
                    )
                    check(
                        "trusted-catalog-remains-unchanged",
                        self.identities.read_bytes() == catalog,
                    )
                    check(
                        "reader-cannot-create",
                        role_request(
                            reader,
                            "POST",
                            f"{mount}/data/admission/forbidden",
                            {"options": {"cas": 0}, "data": {"value": "x"}},
                        ).status_code
                        == 403,
                    )
                    check(
                        "writer-cannot-overwrite-or-delete",
                        all(
                            role_request(writer, method, path, body).status_code == 403
                            for method, path, body in [
                                (
                                    "POST",
                                    f"{mount}/data/admission/{key}",
                                    {
                                        "options": {"cas": 1},
                                        "data": {"value": "changed"},
                                    },
                                ),
                                ("DELETE", f"{mount}/data/admission/{key}", None),
                                ("LIST", f"{mount}/metadata/admission", None),
                            ]
                        ),
                    )
                    # A new principal revision yields a new record. Drop only the write response after the native server commits it.
                    self.directory["principals"]["a"]["revision"] += 1
                    self.save()
                    dropped = [False]

                    class DropResponse(httpx.AsyncBaseTransport):
                        async def handle_async_request(self, request):
                            async with httpx.AsyncHTTPTransport() as transport:
                                response = await transport.handle_async_request(request)
                                raw = await response.aread()
                                if (
                                    request.method == "POST"
                                    and response.status_code == 200
                                    and not dropped[0]
                                ):
                                    dropped[0] = True
                                    raise httpx.ReadTimeout(
                                        "Qualification drops committed write response",
                                        request=request,
                                    )
                                return httpx.Response(
                                    response.status_code,
                                    content=raw,
                                    headers=response.headers,
                                )

                    actual = OpenBaoSecrets
                    with patch(
                        "app.runtime.source_intake.OpenBaoSecrets",
                        side_effect=lambda config: actual(
                            config, transport=DropResponse()
                        ),
                    ):
                        binding = await self.authenticate()
                    check(
                        "committed-write-response-loss-recovered",
                        dropped[0] and await self.authenticate() == binding,
                    )
                    run = await self.submit_as("a")
                    context = await self.context(run)
                    await self.verifier(context, "execute")
                    check(
                        "auto-enrolled-source-admits-E2AG-work",
                        run["authorization"]["expires_at"]
                        <= external["reference"]["expires_at"],
                    )
                    await asyncio.to_thread(
                        admin, "POST", "auth/token/revoke", {"token": writer}
                    )
                    check(
                        "registered-identity-survives-writer-token-revocation",
                        await self.authenticate() == binding,
                    )
                    await self.verifier(context, "execute")
                    issue(policies[1], root / "writer")
                    key = binding["upstream"]["ref"].split("/")[1]
                    await asyncio.to_thread(
                        admin,
                        "POST",
                        f"{mount}/delete/admission/{key}",
                        {"versions": [1]},
                    )
                    with self.assertRaisesRegex(
                        RuntimeErrorCode, "AUTH_PRINCIPAL_REVOKED"
                    ):
                        await self.verifier(context, "execute")
                    check(
                        "native-record-deletion-revokes-work",
                        (await view(self.console.store, run["id"]))["state"]
                        == "revoked",
                    )
                    with self.assertRaisesRegex(
                        RuntimeErrorCode, "AUTH_PROVIDER_UNAVAILABLE"
                    ):
                        await self.authenticate()
                    check(
                        "deleted-source-cannot-be-reenrolled",
                        (
                            await asyncio.to_thread(
                                admin, "GET", f"{mount}/metadata/admission/{key}"
                            )
                        )["data"]["current_version"]
                        == 1,
                    )
                    await asyncio.to_thread(
                        admin,
                        "POST",
                        f"{mount}/undelete/admission/{key}",
                        {"versions": [1]},
                    )
                    with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
                        await self.verifier(context, "execute")
                    check("undelete-does-not-revive-revoked-E2AG-work")
                    check(
                        "raw-source-not-in-runtime-history",
                        access
                        not in json.dumps(await self.console.store.get(run["id"])),
                    )
                    # Native scheduled deletion is tested on a separate synthetic record, leaving source expiry semantics unchanged.
                    await asyncio.to_thread(
                        admin, "POST", f"{mount}/config", {"delete_version_after": "2s"}
                    )
                    ref = {
                        "mount": mount,
                        "path": "admission/retention",
                        "version": 1,
                        "field": "value",
                    }
                    await OpenBaoSecrets(intake["writer"]).create_once_async(
                        ref, {"value": "synthetic-retention"}
                    )
                    await asyncio.sleep(2.2)
                    with self.assertRaisesRegex(
                        RuntimeErrorCode, "SOURCE_SECRET_UNAVAILABLE"
                    ):
                        await OpenBaoSecrets(store).read_async(ref)
                    check("native-retention-makes-expired-record-unreadable")

            result = unittest.TextTestRunner(verbosity=2).run(
                unittest.defaultTestLoader.loadTestsFromTestCase(NativeIntake)
            )
            if not result.wasSuccessful():
                raise SystemExit(1)
            Path(args.output).write_text(
                json.dumps(
                    {
                        "checks": checks,
                        "checks_passed": len(checks),
                        "scope": "Actual OpenBao KV CAS/ACL/retention and IdP introspection; isolated Runtime API/E2AG fixture",
                        "recorded_at_utc": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            for token in issued:
                admin("POST", "auth/token/revoke", {"token": token})
            admin("DELETE", "sys/mounts/" + mount)
            for policy in policies:
                admin("DELETE", "sys/policies/acl/" + policy)


if __name__ == "__main__":
    main()
