"""G03: real MCP reads and loopback outbound effects stop at the E2AG gate."""

import asyncio
import hashlib
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import test_runtime_worker as fixtures
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.database import RuntimeErrorCode
from app.runtime.mcp_tool_transport import McpHttpTransport
from app.runtime.store import digest
from app.runtime.tool_catalog import FileApprovers, FileToolCatalog
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.tool_schema import invocations
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from sqlalchemy import select
from work_auth_fixtures import policy_fixture

OBSERVATIONS = []


class NativeRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.WorkerTests()
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        f = self.f
        self.source = f.root / "source.txt"
        self.source.write_text("合成授权资料🙂", encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).with_name("runtime_mcp_revocation_fixture.py")),
            str(f.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.addAsyncCleanup(self.stop_server)
        self.server = json.loads(
            await asyncio.wait_for(self.process.stdout.readline(), 5)
        )
        self.policy, _ = policy_fixture(f.root, "test-profile", f.spec)
        tools = {}
        for name, action in [("read", "repository.read"), ("send", "artifact.publish")]:
            tool = {
                "enabled": True,
                "transport": "mcp-http",
                "url": f"http://127.0.0.1:{self.server['port']}/mcp",
                "name": name,
                "approval_required": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "result_schema": {
                    "type": "object",
                    "properties": {"content": {"type": "array"}},
                    "required": ["content"],
                },
            }
            tool["permission"] = {
                "action": action,
                "resource_kind": "mcp_tool",
                "resource_id": "fixture/" + name,
                "resource_version": digest(
                    {k: v for k, v in tool.items() if k != "enabled"}
                ),
                "constraint_revision": "test-policy",
            }
            tools[name] = tool
        config = json.loads(self.policy.path.read_text())
        for permissions in config["profiles"]["test-profile"]["permissions"].values():
            permissions.extend(tool["permission"] for tool in tools.values())
        self.policy.path.write_text(json.dumps(config))
        catalog = f.root / "tools.json"
        catalog.write_text(json.dumps({"version": 1, "tools": tools}))
        approvers = f.root / "approvers.json"
        approvers.write_text(
            json.dumps(
                {
                    "version": 1,
                    "approvers": [
                        {
                            "enabled": True,
                            "subject": "operator:independent",
                            "token_sha256": hashlib.sha256(
                                b"fixture-approval"
                            ).hexdigest(),
                            "scopes": [f.scope],
                            "tools": list(tools),
                        }
                    ],
                }
            )
        )
        descriptor = self.policy.admission("test-profile", f.spec.fingerprint(), [])
        self.run = await f.store.submit(
            "revocation",
            {
                "runtime_profile": "test-profile",
                "profile_digest": f.spec.fingerprint(),
                "secret_refs": [],
            },
            authorization=descriptor,
        )
        await f.store.register_worker("worker", "instance", "host", os.getpid())
        self.attempt = await f.store.claim("worker", "instance")
        self.binding = (
            self.run["id"],
            self.attempt["id"],
            "worker",
            "instance",
            self.attempt["epoch"],
        )
        context = WorkAuthorizationContext(
            f.scope,
            *self.binding[:2],
            "worker",
            "instance",
            self.attempt["epoch"],
            self.attempt["authorization_epoch"],
            None,
            self.run["input"]["work_authorization_ref"],
        )
        verifier = WorkAuthorizationVerifier(f.store, self.policy)
        await verifier(context, "execute")
        await f.store.report(*self.binding, 1, "preparing")
        await f.store.report(*self.binding, 2, "creating_session")
        await verifier(replace(context, session_id="session-one"), "execute")
        await f.store.report(
            *self.binding, 3, "waiting_model", session_id="session-one"
        )
        self.gate = ToolInvocations(
            f.store,
            self.policy,
            FileToolCatalog(catalog),
            f.provider,
            McpHttpTransport(),
            FileApprovers(approvers),
        )

    async def stop_server(self):
        if self.process.returncode is None:
            self.process.terminate()
        await asyncio.wait_for(self.process.communicate(), 5)

    def events(self):
        path = self.f.root / "events.jsonl"
        return (
            [json.loads(x) for x in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    async def prepare(self, name, key):
        item = await self.gate.request(
            self.run["id"],
            self.attempt["id"],
            "session-one",
            name,
            {"text": "合成成果🙂"},
            key,
        )
        return await self.gate.decide(
            self.run["id"], item["id"], item["version"], "approved", "fixture-approval"
        )

    async def verify_revocation(self, during_initialize):
        # Positive controls prove both real operations are reachable under the original grant.
        for name in ["read", "send"]:
            item = await self.prepare(name, "allowed-" + name)
            value = await self.gate.dispatch(
                self.run["id"], item["id"], item["version"]
            )
            self.assertEqual(value["state"], "succeeded")
        before = self.events()
        self.assertEqual(
            [x["effect"] for x in before if "effect" in x], ["read", "outbound"]
        )
        items = [
            await self.prepare(name, "blocked-" + name) for name in ["read", "send"]
        ]

        async def dispatch(item):
            with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
                await self.gate.dispatch(self.run["id"], item["id"], item["version"])

        pending = []
        try:
            if during_initialize:
                (self.f.root / "hold").touch()
                pending = [asyncio.create_task(dispatch(item)) for item in items]
                async with asyncio.timeout(5):
                    while (
                        len(
                            [
                                x
                                for x in self.events()
                                if x.get("barrier") == "initialize"
                            ]
                        )
                        < 2
                    ):
                        await asyncio.sleep(0.01)
            live = await self.f.store.get(self.run["id"])
            self.assertEqual(live["attempts"][0]["state"], "waiting_model")
            await self.f.store.revoke(live["id"], live["version"])
            revoked = await self.f.store.get(live["id"])
            (self.f.root / "release").touch()
            if not pending:
                pending = [asyncio.create_task(dispatch(item)) for item in items]
            await asyncio.wait_for(asyncio.gather(*pending), 5)
            denied = []
            for item in items:
                result = await self.gate.get(self.run["id"], item["id"])
                async with self.f.db.read(self.f.scope) as connection:
                    binding = (
                        await connection.execute(
                            select(invocations.c.binding).where(
                                invocations.c.id == item["id"],
                                invocations.c.scope == self.f.scope,
                            )
                        )
                    ).scalar_one()
                self.assertEqual(binding["run_id"], self.run["id"])
                self.assertEqual(binding["attempt_id"], self.attempt["id"])
                self.assertEqual(binding["lease_epoch"], self.attempt["epoch"])
                self.assertEqual(
                    binding["authorization_epoch"], self.attempt["authorization_epoch"]
                )
                self.assertEqual(binding["session_id"], "session-one")
                self.assertEqual(
                    binding["authorization_ref"],
                    self.run["input"]["work_authorization_ref"],
                )
                self.assertEqual(result["state"], "prepared")
                self.assertEqual(result["approval_state"], "approved")
                evidence = [
                    x for x in result["evidence"] if x["decision"]["decision"] == "deny"
                ]
                self.assertTrue(
                    any(
                        "AUTH_REVOKED" in x["decision"]["reason_codes"]
                        for x in evidence
                    )
                )
                denied.append(
                    {
                        "invocation_id": item["id"],
                        "tool_id": item["tool_id"],
                        "binding": binding,
                        "permission": result["permission"],
                        "denials": evidence,
                    }
                )
                with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
                    await self.gate.request(
                        self.run["id"],
                        self.attempt["id"],
                        "session-one",
                        item["tool_id"],
                        {"text": "新请求"},
                        "new-" + item["tool_id"],
                    )
            after = self.events()
            self.assertEqual(
                [x for x in after if x.get("method") == "tools/call"],
                [x for x in before if x.get("method") == "tools/call"],
            )
            self.assertEqual(
                [x for x in after if "effect" in x],
                [x for x in before if "effect" in x],
            )
            self.assertEqual(self.source.read_text(encoding="utf-8"), "合成授权资料🙂")
            self.assertIsNone(self.process.returncode)
            OBSERVATIONS.append(
                {
                    "case": "G03",
                    "injection": "during-initialize"
                    if during_initialize
                    else "before-dispatch",
                    "endpoint_pid": self.server["pid"],
                    "transport": "production McpHttpTransport over loopback TCP",
                    "harness_execution": "task/attempt/session entered through production store APIs; no model invoked",
                    "allowed_tools": ["read", "send"],
                    "allowed_effects": ["read", "outbound"],
                    "calls_after_revoke": 0,
                    "new_requests_denied": 2,
                    "initialization_barriers": sum(
                        x.get("barrier") == "initialize" for x in after
                    ),
                    "run_id": self.run["id"],
                    "attempt_id": self.attempt["id"],
                    "revoked_run_version": revoked["version"],
                    "source_sha256": hashlib.sha256(
                        self.source.read_bytes()
                    ).hexdigest(),
                    "denied": denied,
                }
            )
        finally:
            (self.f.root / "release").touch()
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def test_revoked_work_blocks_prepared_and_new_read_and_outbound(self):
        await self.verify_revocation(False)

    async def test_revoke_during_mcp_initialize_blocks_read_and_outbound(self):
        await self.verify_revocation(True)
