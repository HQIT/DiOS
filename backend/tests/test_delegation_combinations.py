"""Three authenticated issuers, native OPA/MCP and managed source revocation."""

import asyncio
import copy
import hashlib
import secrets
from dataclasses import replace

from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.database import RuntimeErrorCode
from app.runtime.principal_updates import update
from app.runtime.work_authorizations import view
from test_runtime_delegation_identity import DelegationIdentityTests

OBSERVATIONS = []


class DelegationCombinationTests(DelegationIdentityTests):
    async def authenticated_chain(self):
        self.directory["principals"]["peer"]["actor"] = self.body["actor"]
        self.tokens["third"] = secrets.token_urlsafe(32)
        third = copy.deepcopy(self.directory["principals"]["peer"])
        third["subject"] = "subject:third"
        third["actor"] = "delegate:second-authenticated"
        third["tokens"][0]["sha256"] = hashlib.sha256(
            self.tokens["third"].encode()
        ).hexdigest()
        self.directory["principals"]["third"] = third
        self.save_identity()
        first = await self.issue_api()
        chain = [self.chain[0], first]
        for name, actor in [("peer", third["actor"]), ("third", self.body["actor"])]:
            body = dict(
                self.body,
                idempotency_key="via-" + name,
                parent_ref=chain[-1]["id"],
                actor=actor,
                expires_at=chain[-1]["expires_at"] - 10,
            )
            response = await self.http.post(
                "/api/os/runtime/delegations", headers=self.headers(name), json=body
            )
            self.assertEqual(response.status_code, 201, response.text)
            chain.append(response.json())
        return chain

    async def claim_leaf(self, leaf):
        original_run = self.run
        self.run = await self.bind_api_work(leaf)
        await self.store.register_worker("qualified-leaf", "boot", "host", 123)
        self.attempt = await self.store.claim("qualified-leaf", "boot")
        self.assertEqual(self.attempt["run_id"], self.run["id"])
        self.binding = (
            self.run["id"],
            self.attempt["id"],
            "qualified-leaf",
            "boot",
            self.attempt["epoch"],
        )
        self.context = WorkAuthorizationContext(
            self.scope,
            self.run["id"],
            self.attempt["id"],
            "qualified-leaf",
            "boot",
            self.attempt["epoch"],
            self.attempt["authorization_epoch"],
            None,
            self.run["input"]["work_authorization_ref"],
        )
        await self.verifier(self.context, "execute")
        await self.store.report(*self.binding, 1, "preparing")
        await self.store.report(*self.binding, 2, "creating_session")
        await self.verifier(replace(self.context, session_id="session-one"), "execute")
        await self.store.report(
            *self.binding, 3, "waiting_model", session_id="session-one"
        )
        return original_run

    async def test_middle_issuer_revoke_fences_leaf_approval_and_new_admission(self):
        chain = await self.authenticated_chain()
        peer_run = await self.claim_leaf(chain[-1])
        item = await self.approve(await self.request())
        before = await view(self.store, self.run["id"])
        self.assertEqual(
            [x["id"] for x in before["delegation"]["chain"]], [x["id"] for x in chain]
        )
        mutation = await update(
            self.store, self.identity_path, "peer", 1, {"enabled": False}
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.gate.dispatch(self.run["id"], item["id"], item["version"])
        self.assertEqual(self.calls, [])
        after = await view(self.store, self.run["id"])
        self.assertEqual(
            (after["state"], after["grant"]["state"]), ("revoked", "revoked")
        )
        self.assertEqual((await view(self.store, peer_run["id"]))["state"], "active")
        self.assertEqual(after["delegation"]["chain"][2]["state"], "revoked")
        self.assertEqual(after["delegation"]["chain"][2]["captured_epoch"], 1)
        self.assertEqual(after["delegation"]["chain"][2]["authorization_epoch"], 2)
        await update(self.store, self.identity_path, "peer", 2, {"enabled": True})
        with self.assertRaisesRegex(RuntimeErrorCode, "DELEGATION_REVOKED"):
            await self.bind_api_work(chain[-1], "after-source-restored")
        OBSERVATIONS.append(
            {
                "case": self._testMethodName,
                "chain": [x["id"] for x in chain],
                "run_id": self.run["id"],
                "attempt_id": self.attempt["id"],
                "approval_id": item["id"],
                "source_update": mutation,
                "tool_calls": 0,
                "unrelated_work_preserved": True,
                "restoration_did_not_revive": True,
            }
        )

    async def test_three_level_concurrent_dispatch_consumes_one_approval(self):
        chain = await self.authenticated_chain()
        await self.claim_leaf(chain[-1])
        item = await self.approve(await self.request())
        results = await asyncio.gather(
            *(
                self.gate.dispatch(self.run["id"], item["id"], item["version"])
                for _ in range(3)
            )
        )
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(x["id"] == item["id"] for x in results))
        self.assertEqual(
            (await self.gate.get(self.run["id"], item["id"]))["state"], "succeeded"
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "SESSION_CHANGED"):
            await self.gate.request(
                self.run["id"],
                self.attempt["id"],
                "replacement-session",
                "search",
                {"query": "evidence"},
                "replacement",
            )
        self.assertEqual(len(self.calls), 1)
        OBSERVATIONS.append(
            {
                "case": self._testMethodName,
                "chain": [x["id"] for x in chain],
                "run_id": self.run["id"],
                "attempt_id": self.attempt["id"],
                "approval_id": item["id"],
                "tool_calls": 1,
                "concurrent_dispatches": 3,
                "replacement_session_denied": True,
            }
        )
