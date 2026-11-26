"""R14: real Linux systemd services, repaired PATH/proxy, and redacted journals."""

import asyncio
import base64
import json
import os
import secrets
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from app.runtime.acp_preflight import check_environment
from app.runtime.database import RuntimeErrorCode
from app.runtime.faults import category

ROOT = Path(__file__).resolve().parents[2]
OBSERVATIONS = []


class PreflightSpecificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_specifications_fail_before_any_network(self):
        for value in (
            {"commands": ["git --version"]},
            {"commands": ["/usr/bin/git"]},
            {"network": [{"id": "check", "url": "http://secret:token@127.0.0.1"}]},
            {
                "network": [
                    {"id": "check", "url": "https://example.invalid/?token=private"}
                ]
            },
            {
                "network": [
                    {
                        "id": "check",
                        "url": "https://example.invalid",
                        "proxy_env": "bad name",
                    }
                ]
            },
            {"network": [{"id": "check", "url": "http://["}]},
            {"commands": "git"},
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(RuntimeErrorCode, "ACP_CONFIG_INVALID"),
            ):
                await check_environment(value, {})
        await check_environment(None, {})
        self.assertEqual(category("ACP_PREFLIGHT_NETWORK_FAILED"), "network")
        self.assertEqual(category("ACP_PREFLIGHT_PROXY_MISSING"), "configuration")


@unittest.skipUnless(
    sys.platform == "linux" and os.environ.get("DIOS_TEST_SYSTEMD") == "1",
    "explicit isolated Linux user service required",
)
class NativeUnattendedTests(unittest.IsolatedAsyncioTestCase):
    async def command(self, *args, check=True):
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if check:
            self.assertEqual(process.returncode, 0, err.decode()[-1000:])
        return process.returncode, out.decode(), err.decode()

    async def test_service_dependency_proxy_repair_and_no_credential_logs(self):
        await self.command("systemctl", "--user", "is-system-running")
        with tempfile.TemporaryDirectory(prefix="dios-unattended-") as temporary:
            root = Path(temporary)
            envfile, configfile = root / "service.env", root / "acp.json"
            token = "qualification-" + secrets.token_hex(16)
            credential = root / "credential"
            credential.write_text(token)
            credential.chmod(0o600)
            requests = []
            expected_auth = (
                "Basic " + base64.b64encode(("fixture:" + token).encode()).decode()
            )

            class Proxy(BaseHTTPRequestHandler):
                def do_HEAD(self):
                    accepted = (
                        self.path == "http://127.0.0.1:1/qualification"
                        and self.headers.get("Proxy-Authorization") == expected_auth
                    )
                    requests.append({"method": "HEAD", "authenticated": accepted})
                    self.send_response(200 if accepted else 407)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

                def log_message(self, *_):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            config = {
                "command": [
                    sys.executable,
                    str(ROOT / "backend/tests/runtime_acp_fault_fixture.py"),
                    "server",
                ],
                "cwd": str(root),
                "prompt_timeout": 5,
                "credential_env_files": {"MODEL_CANARY": str(credential)},
                "preflight": {
                    "commands": ["git"],
                    "network": [
                        {
                            "id": "model-route",
                            "url": "http://127.0.0.1:1/qualification",
                            "proxy_env": "HTTPS_PROXY",
                        }
                    ],
                },
            }
            journals, observations = [], []
            try:
                for phase in (
                    "missing-git",
                    "missing-proxy",
                    "unreachable-proxy",
                    "repaired",
                ):
                    envfile.write_text(
                        f"PATH={root / 'empty' if phase == 'missing-git' else '/usr/bin:/bin'}\n"
                        f"PYTHONPATH={ROOT / 'backend'}:{ROOT / 'backend/tests'}\n"
                        f"ACP_FAULT_JOURNAL={root / 'prompts.jsonl'}\n"
                    )
                    envfile.chmod(0o600)
                    if phase in {"unreachable-proxy", "repaired"}:
                        proxyfile = root / "proxy"
                        port = 1 if phase == "unreachable-proxy" else server.server_port
                        proxyfile.write_text(f"http://fixture:{token}@127.0.0.1:{port}")
                        proxyfile.chmod(0o600)
                        config["credential_env_files"]["HTTPS_PROXY"] = str(proxyfile)
                    configfile.write_text(json.dumps(config))
                    configfile.chmod(0o600)
                    unit = "dios-qualification-" + uuid4().hex[:16]
                    try:
                        code, out, err = await self.command(
                            "systemd-run",
                            "--user",
                            "--wait",
                            "--unit=" + unit,
                            "--property=EnvironmentFile=" + str(envfile),
                            "--property=UnsetEnvironment=HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy ACP_FAULT_MODE",
                            "--property=WorkingDirectory=" + str(ROOT / "backend"),
                            "--property=Restart=no",
                            "--property=UMask=0077",
                            sys.executable,
                            str(ROOT / "backend/tests/runtime_unattended_fixture.py"),
                            str(configfile),
                            check=False,
                        )
                        _, journal, _ = await self.command(
                            "journalctl",
                            "--user",
                            "-u",
                            unit,
                            "--no-pager",
                            "-o",
                            "cat",
                        )
                        journals.append(journal + out + err)
                        expected = {
                            "missing-git": "ACP_PREFLIGHT_DEPENDENCY_MISSING",
                            "missing-proxy": "ACP_PREFLIGHT_PROXY_MISSING",
                            "unreachable-proxy": "ACP_PREFLIGHT_NETWORK_FAILED",
                        }.get(phase)
                        if expected:
                            self.assertNotEqual(code, 0)
                            self.assertIn(expected, journal)
                            self.assertIn(
                                '"check": "git"'
                                if phase == "missing-git"
                                else '"check": "model-route"',
                                journal,
                            )
                            self.assertFalse((root / "prompts.jsonl").exists())
                        else:
                            self.assertEqual(code, 0, journal)
                            self.assertIn('"type":"result"', journal.replace(" ", ""))
                            prompts = [
                                json.loads(line)
                                for line in (root / "prompts.jsonl")
                                .read_text()
                                .splitlines()
                            ]
                            self.assertEqual(len(prompts), 1)
                            observations.append(
                                {
                                    "session": prompts[0]["session"],
                                    "pid": prompts[0]["pid"],
                                }
                            )
                        observations.append(
                            {
                                "phase": phase,
                                "unit": unit,
                                "code": expected or "succeeded",
                            }
                        )
                    finally:
                        await self.command(
                            "systemctl", "--user", "stop", unit, check=False
                        )
                        await self.command(
                            "systemctl", "--user", "reset-failed", unit, check=False
                        )
                for value in (token, expected_auth, "http://fixture:"):
                    self.assertNotIn(value, "\n".join(journals))
                self.assertEqual(requests, [{"method": "HEAD", "authenticated": True}])
                OBSERVATIONS.append(
                    {
                        "case": "R14",
                        "environment": "Linux systemd user services",
                        "phases": observations,
                        "authenticated_proxy_probe": True,
                        "credentials_absent_from_journal": True,
                        "prompt_calls": 1,
                        "model": "synthetic ACP endpoint",
                        "preflight_before_session_creation": True,
                    }
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
