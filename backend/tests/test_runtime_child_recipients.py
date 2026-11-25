"""Real OPA and authenticated HTTP; no caller-controlled actor or owner tuple."""

import asyncio
import json
import os
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.child_handoffs import required as handoff_binding
from app.runtime.database import RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.policy_evaluator import OpaEvaluator
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.tool_channel_client import ToolChannelClient
from app.runtime.work_auth_schema import work
from app.runtime.work_authorizations import WorkAuthorizationVerifier, view
from app.runtime.work_policy import permission
from app.runtime.worker import Worker, read_profile
from app.runtime.worker_tool_channel import WorkerToolChannel
from sqlalchemy import select
from test_runtime_delegated_submission import DelegatedSubmissionTests
from test_runtime_worker import FakeAdapter, protocol


class ChildRecipientTests(DelegatedSubmissionTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        child_path = self.profiles / "one.json"
        parent_path = self.profiles / "parent.json"
        raw = json.loads(child_path.read_text())
        raw["launch"] = {
            "image": "sha256:78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db",
            "command": [
                "python",
                "-u",
                "-c",
                "import sys,time;sys.stdout.buffer.write("
                + repr(protocol(complete=False))
                + ");sys.stdout.flush();time.sleep(5);sys.stdout.buffer.write("
                + repr(protocol().splitlines(keepends=True)[-1])
                + ");sys.stdout.flush()",
            ],
        }
        child_path.write_text(json.dumps(raw))
        _, self.spec = read_profile(child_path)
        self.permissions = [
            dict(p, resource_version=self.spec.fingerprint())
            if p["resource_kind"] == "runtime_profile"
            and p["resource_id"] == "test-profile"
            else p
            for p in self.permissions
        ]
        self.body["permissions"] = self.permissions
        for principal in self.directory["principals"].values():
            principal["profiles"]["test-profile"]["permissions"] = self.permissions
        raw["id"] = "parent-profile"
        self.channel_policy = {
            "tools": {},
            "acp_names": {},
            "children": {
                "profiles": {
                    "review": {
                        "path": str(child_path),
                        "profile_id": "test-profile",
                        "profile_digest": self.spec.fingerprint(),
                        "authenticated_recipient": True,
                    }
                },
                "max_children": 2,
                "max_depth": 2,
            },
        }
        raw["tool_channel"] = self.channel_policy
        parent_path.write_text(json.dumps(raw))
        _, self.parent_spec = read_profile(parent_path)
        config = json.loads(self.policy.path.read_text())
        config["profiles"]["test-profile"]["profile_digest"] = self.spec.fingerprint()
        config["profiles"]["test-profile"]["permissions"] = {
            layer: list(self.permissions)
            for layer in config["profiles"]["test-profile"]["permissions"]
        }
        parent_grant = dict(
            config["profiles"]["test-profile"],
            profile_digest=self.parent_spec.fingerprint(),
        )
        desc = {
            "profile_id": "parent-profile",
            "profile_digest": self.parent_spec.fingerprint(),
            "policy_id": config["policy_id"],
        }
        child_desc = dict(
            desc, profile_id="test-profile", profile_digest=self.spec.fingerprint()
        )
        self.parent_permissions = [
            *self.permissions,
            permission(desc, "execute"),
            permission(desc, "accept_result"),
            permission(child_desc, "spawn"),
            permission(child_desc, "read_result"),
        ]
        parent_grant["permissions"] = {
            layer: list(self.parent_permissions)
            for layer in parent_grant["permissions"]
        }
        self.root_link = await self.manager.issue(
            "handoff-root",
            subject=self.chain[0]["subject_ref"],
            actor=self.chain[0]["actor_ref"],
            permissions=self.parent_permissions,
            expires_at=self.deadline,
            policy_evaluator=self.config,
        )
        parent_grant["delegation_ref"] = self.root_link["id"]
        config["profiles"]["parent-profile"] = parent_grant
        config["profiles"]["test-profile"]["delegation_ref"] = self.root_link["id"]
        self.policy.path.write_text(json.dumps(config))
        owner = self.directory["principals"]["issuer"]
        owner["subject"] = self.root_link["subject_ref"]
        owner["profiles"]["parent-profile"] = {
            "actions": ["run.submit", "run.read", "artifact.read"],
            "permissions": self.parent_permissions,
        }
        self.save_identity()
        response = await self.http.post(
            "/api/os/runtime/runs",
            headers=self.headers(),
            json={
                "profile_id": "parent-profile",
                "sample_id": "one",
                "goal": "coordinate",
                "idempotency_key": "parent",
                "delegation_ref": self.root_link["id"],
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.parent = response.json()
        self.body.update(
            parent_ref=self.root_link["id"], idempotency_key="recipient-leaf"
        )
        wid = str(uuid4())
        await self.store.register_worker(wid, "instance", "host", os.getpid())
        a = await self.store.claim(wid, "instance", profile_id="parent-profile")
        self.owner = (self.parent["id"], a["id"], wid, "instance", a["epoch"])
        current = await self.store.get(self.parent["id"])
        context = WorkAuthorizationContext(
            self.scope,
            self.parent["id"],
            a["id"],
            wid,
            "instance",
            a["epoch"],
            a["authorization_epoch"],
            None,
            current["input"]["work_authorization_ref"],
        )
        self.verifier = WorkAuthorizationVerifier(self.store, self.policy)
        await self.verifier(context, "execute")
        await self.store.report(*self.owner, 1, "preparing")
        await self.store.report(*self.owner, 2, "creating_session")
        self.parent_session = "parent-session"
        context = replace(context, session_id=self.parent_session)
        await self.verifier(context, "execute")
        await self.store.report(
            *self.owner, 3, "waiting_model", session_id=self.parent_session
        )
        self.body["parent_run_id"] = self.parent["id"]
        self.child = await self.issue_api()
        self.request_body.update(
            delegation_ref=self.child["id"], parent_run_id=self.parent["id"]
        )

    async def test_independent_recipient_binds_parent_executes_and_replays(self):
        response = await self.submit_http()
        self.assertEqual(response.status_code, 202, response.text)
        child = await self.store.get(response.json()["id"])
        self.assertEqual(child["input"]["principal"]["actor"], "actor:peer")
        self.assertEqual(child["family"]["parent"]["parent_run_id"], self.parent["id"])
        self.assertLessEqual(
            (await view(self.store, child["id"]))["expires_at"],
            (await view(self.store, self.parent["id"]))["expires_at"],
        )
        adapter = FakeAdapter()
        w = Worker(
            self.store,
            adapter,
            "test-profile",
            self.spec,
            self.provider,
            authorization_verifier=self.verifier,
            poll_interval=0.05,
        )
        result = await w.once()
        self.assertEqual(result["state"], "succeeded")
        parent = await self.store.get(self.parent["id"])
        await self.store.cancel(parent["id"], parent["version"])
        replay = await self.submit_http()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["id"], child["id"])
        self.assertEqual(adapter.started, 1)

    async def test_unrelated_leaf_and_local_admin_identity_are_rejected(self):
        response = await self.submit_http(
            dict(self.request_body, delegation_ref=self.chain[-1]["id"])
        )
        self.assertNotEqual(response.status_code, 202)
        response = await self.submit_http(
            headers={"X-DiOS-Access-Token": "test-operator"}
        )
        self.assertEqual(response.json()["detail"], "AUTH_PRINCIPAL_REQUIRED")
        response = await self.submit_http(
            dict(self.request_body, parent_execution={"owner": list(self.owner)})
        )
        self.assertEqual(response.status_code, 422)

    async def test_other_issuer_with_same_actor_cannot_attach_its_delegation(self):
        # Same actor label is insufficient: the actual issuing principal must match.
        self.directory["principals"]["peer"]["actor"] = self.root_link["actor_ref"]
        self.save_identity()
        body = dict(self.body, idempotency_key="other-issuer", actor="actor:peer")
        issued = await self.http.post(
            "/api/os/runtime/delegations", headers=self.headers("peer"), json=body
        )
        self.assertEqual(issued.status_code, 409, issued.text)
        self.assertEqual(issued.json()["detail"], "CHILD_RECIPIENT_BINDING_MISMATCH")

    async def test_general_delegation_cannot_attach_and_bound_delegation_cannot_escape_tree(
        self,
    ):
        general = dict(self.body, idempotency_key="general")
        general.pop("parent_run_id")
        unbound = await self.issue_api(general)
        response = await self.submit_http(
            dict(self.request_body, delegation_ref=unbound["id"])
        )
        self.assertEqual(response.json()["detail"], "CHILD_HANDOFF_REQUIRED")
        standalone = dict(self.request_body)
        standalone.pop("parent_run_id")
        response = await self.submit_http(standalone)
        self.assertEqual(response.json()["detail"], "CHILD_PARENT_REQUIRED")

    async def test_same_issuer_other_live_parent_cannot_reuse_handoff(self):
        response = await self.http.post(
            "/api/os/runtime/runs",
            headers=self.headers(),
            json={
                "profile_id": "parent-profile",
                "sample_id": "one",
                "goal": "other parent",
                "idempotency_key": "other-parent",
                "delegation_ref": self.root_link["id"],
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        other = await self.store.get(response.json()["id"])
        wid = str(uuid4())
        await self.store.register_worker(wid, "instance", "host", os.getpid())
        attempt = await self.store.claim(wid, "instance", profile_id="parent-profile")
        owner = (other["id"], attempt["id"], wid, "instance", attempt["epoch"])
        context = WorkAuthorizationContext(
            self.scope,
            other["id"],
            attempt["id"],
            wid,
            "instance",
            attempt["epoch"],
            attempt["authorization_epoch"],
            None,
            other["input"]["work_authorization_ref"],
        )
        await self.verifier(context, "execute")
        await self.store.report(*owner, 1, "preparing")
        await self.store.report(*owner, 2, "creating_session")
        context = replace(context, session_id="other-session")
        await self.verifier(context, "execute")
        await self.store.report(*owner, 3, "waiting_model", session_id="other-session")
        response = await self.submit_http(
            dict(self.request_body, parent_run_id=other["id"])
        )
        self.assertEqual(response.json()["detail"], "CHILD_RECIPIENT_BINDING_MISMATCH")

    async def test_parent_cancel_blocks_new_recipient_and_cancels_queued_child(self):
        response = await self.submit_http()
        self.assertEqual(response.status_code, 202, response.text)
        parent = await self.store.get(self.parent["id"])
        await self.store.cancel(parent["id"], parent["version"])
        self.assertEqual(
            (await self.store.get(response.json()["id"]))["state"], "cancelled"
        )
        response = await self.submit_http(
            dict(self.request_body, idempotency_key="second")
        )
        self.assertNotEqual(response.status_code, 202)

    async def test_recipient_permission_loss_prevents_worker_start(self):
        response = await self.submit_http()
        self.assertEqual(response.status_code, 202, response.text)
        self.directory["principals"]["peer"]["enabled"] = False
        self.save_identity()
        adapter = FakeAdapter()
        w = Worker(
            self.store,
            adapter,
            "test-profile",
            self.spec,
            self.provider,
            authorization_verifier=self.verifier,
            poll_interval=0.05,
        )
        result = await w.once()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(adapter.started, 0)

    async def test_result_return_requires_explicit_parent_permission_and_live_owner(
        self,
    ):
        response = await self.submit_http()
        self.assertEqual(response.status_code, 202, response.text)
        cid = response.json()["id"]
        w = Worker(
            self.store,
            FakeAdapter(),
            "test-profile",
            self.spec,
            self.provider,
            authorization_verifier=self.verifier,
            poll_interval=0.05,
        )
        self.assertEqual((await w.once())["state"], "succeeded")
        # Delegating a task does not grant general API access to another identity.
        external = await self.http.get(
            "/api/os/runtime/runs/" + cid, headers=self.headers()
        )
        self.assertEqual(external.status_code, 404)
        gate = SimpleNamespace(
            store=self.store,
            policy=self.policy,
            provider=self.provider,
            identity_guard=None,
        )
        channel = WorkerToolChannel(gate, self.owner, self.channel_policy, os.getuid())
        await channel.open()
        await channel.bind_session(self.parent_session)
        try:
            client = ToolChannelClient(str(channel.directory / "channel.sock"))
            observed = await client.child(cid)
            self.assertEqual(observed["result"]["summary"], "合成结果")
            self.assertEqual([r['id'] for r in (await client.children())['items']], [cid])
            async with self.db.transaction(self.scope) as c:
                authority = (
                    (
                        await c.execute(
                            select(work).where(work.c.run_id == self.parent["id"])
                        )
                    )
                    .mappings()
                    .one()
                )
                perms = {
                    k: [p for p in rows if p["action"] != "runtime.read_result"]
                    for k, rows in authority["permissions"].items()
                }
                await c.execute(
                    work.update()
                    .where(work.c.id == authority["id"])
                    .values(permissions=perms)
                )
            with self.assertRaisesRegex(RuntimeErrorCode, "CHILD_RESULT_NOT_GRANTED"):
                await client.child(cid)
            with self.assertRaisesRegex(RuntimeErrorCode, "CHILD_RESULT_NOT_GRANTED"):
                await client.children()
            parent = await self.store.get(self.parent["id"])
            await self.store.cancel(parent["id"], parent["version"])
            with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
                await client.child(cid)
        finally:
            await channel.close()

    async def test_withdrawal_during_opa_wait_rolls_back_recipient_admission(self):
        before = await self.count_runs()
        original = OpaEvaluator.evaluate
        owner = self

        async def change_after_wait(instance, *args):
            result = await original(instance, *args)
            owner.directory["principals"]["peer"]["enabled"] = False
            owner.save_identity()
            return result

        with patch.object(OpaEvaluator, "evaluate", change_after_wait):
            response = await self.submit_http()
        self.assertEqual(response.json()["detail"], "AUTH_PRINCIPAL_REVOKED")
        self.assertEqual(await self.count_runs(), before)

    async def test_duplicate_handoffs_share_receipt_and_tree_limit(self):
        before = await self.count_runs()
        responses = await asyncio.gather(*(self.submit_http() for _ in range(3)))
        self.assertTrue(
            all(r.status_code == 202 for r in responses), [r.text for r in responses]
        )
        self.assertEqual(len({r.json()["id"] for r in responses}), 1)
        self.assertEqual(
            (
                await self.submit_http(
                    dict(self.request_body, idempotency_key="second")
                )
            ).status_code,
            202,
        )
        third = await self.submit_http(dict(self.request_body, idempotency_key="third"))
        self.assertEqual(third.json()["detail"], "CHILD_LIMIT_EXCEEDED")
        self.assertEqual(await self.count_runs(), before + 2)

    async def test_parent_profile_opt_in_is_required(self):
        path = self.profiles / "parent.json"
        raw = json.loads(path.read_text())
        raw["tool_channel"]["children"]["profiles"]["review"][
            "authenticated_recipient"
        ] = False
        path.write_text(json.dumps(raw))
        response = await self.submit_http()
        self.assertEqual(response.json()["detail"], "PROFILE_CHANGED")

    async def test_real_docker_recipient_completion_and_live_parent_revocation(self):
        response = await self.submit_http()
        self.assertEqual(response.status_code, 202, response.text)
        adapter = DockerRuntimeAdapter(self.scope)
        w = Worker(
            self.store,
            adapter,
            "test-profile",
            self.spec,
            self.provider,
            authorization_verifier=self.verifier,
            poll_interval=0.1,
        )
        done = await w.once()
        self.assertEqual(done["state"], "succeeded")
        self.assertGreater(done["attempts"][0]["process_id"], 0)
        self.assertEqual(done["attempts"][0]["session_id"], "fixture-session")
        second = await self.submit_http(
            dict(self.request_body, idempotency_key="second")
        )
        self.assertEqual(second.status_code, 202, second.text)
        pending = asyncio.create_task(w.once())
        try:
            for _ in range(80):
                current = await self.store.get(second.json()["id"])
                if current["attempts"] and current["attempts"][0]["session_id"]:
                    break
                await asyncio.sleep(0.1)
            self.assertTrue((await adapter.observe(current["attempt_id"])).alive)
            parent = await self.store.get(self.parent["id"])
            await self.store.revoke(parent["id"], parent["version"])
            stopped = await asyncio.wait_for(pending, 20)
            self.assertEqual(stopped["state"], "cancelled")
            self.assertFalse((await adapter.observe(current["attempt_id"])).alive)
            self.assertEqual(len(stopped["attempts"]), 1)
            output = os.environ.get("DIOS_TEST_EVIDENCE_DIR")
            if output:
                async with self.db.read(self.scope) as c:
                    proof = await handoff_binding(c, self.scope, self.child["id"])
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                fields = ("id", "state", "attempt_id", "attempts")
                evidence = {
                    "parent_execution": "Runtime owner/session fixture; no parent Harness process",
                    "child_execution": "native Docker process",
                    "image": self.spec.image,
                    "parent_run_id": self.parent["id"],
                    "subject": self.root_link["subject_ref"],
                    "parent_actor": self.root_link["actor_ref"],
                    "child_actor": "actor:peer",
                    "handoff": proof,
                    "completed": {k: done[k] for k in fields},
                    "revoked": {k: stopped[k] for k in fields},
                }
                (directory / ("native-" + uuid4().hex + ".json")).write_text(
                    json.dumps(evidence, indent=2)
                )
        finally:
            if not pending.done():
                parent = await self.store.get(self.parent["id"])
                await self.store.cancel(parent["id"], parent["version"])
                await asyncio.wait_for(pending, 20)

    async def test_backup_keeps_immutable_handoff_and_its_runtime_tree(self):
        response = await self.submit_http()
        self.assertEqual(response.status_code, 202, response.text)
        target = self.root / "handoff-backup"
        self.provider.root.mkdir(exist_ok=True)
        await asyncio.to_thread(backup_sqlite, 'sqlite+aiosqlite:///'+str(self.root/'runtime.sqlite'), self.provider.root, target)
        report = await asyncio.to_thread(verify_backup, target)
        self.assertIn("runtime_child_handoff_schema", report["schemas"])
        with sqlite3.connect(target / "state.sqlite") as c:
            saved = c.execute(
                "select binding from runtime_child_handoffs where delegation_ref=?",
                (self.child["id"],),
            ).fetchone()
            relation = c.execute(
                "select parent_run_id from runtime_child_runs where run_id=?",
                (response.json()["id"],),
            ).fetchone()
        self.assertEqual(
            json.loads(saved[0])["parent_execution"]["owner"], list(self.owner)
        )
        self.assertEqual(relation[0], self.parent["id"])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        ChildRecipientTests(name)
        for name in ChildRecipientTests.__dict__
        if name.startswith("test_")
    )
