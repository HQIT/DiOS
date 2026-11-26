"""Native execution/exit evidence for the managed identity update boundary."""

import asyncio
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from sqlalchemy import select

from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.principal_updates import update
from app.runtime.principals import FilePrincipals
from app.runtime.worker import Worker, read_profile
from app.runtime.work_authorizations import WorkAuthorizationVerifier, view
from app.runtime.schema import artifacts
from work_auth_fixtures import policy_fixture
from test_runtime_principals import PrincipalTests

OBSERVATIONS = []


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1", "native Docker qualification required"
)
class NativePrincipalUpdateTests(PrincipalTests):
    async def test_update_waits_for_native_exit_receipt(self):
        root = Path(__file__).resolve().parents[2]
        _, self.spec = read_profile(root / "examples/runtime/fixture-profile.json")
        self.policy, descriptor = policy_fixture(self.root, "review", self.spec)
        for row in self.directory["principals"].values():
            row["profiles"]["review"]["permissions"] = descriptor["permissions"][
                "caller"
            ]
        self.save()
        config = json.loads(self.policy.path.read_text())
        config["identity_catalog"] = str(self.identities)
        self.policy.path.write_text(json.dumps(config))
        binding = FilePrincipals(self.identities).authenticate(self.tokens["a"], "one")
        descriptor = self.policy.admission(
            "review", self.spec.fingerprint(), [], principal=binding
        )
        store = self.console.store
        run = await store.submit(
            "native-source-change",
            dict(
                runtime_profile="review",
                profile_digest=self.spec.fingerprint(),
                secret_refs=[],
                principal=binding,
                payload={"delay": 30},
            ),
            authorization=descriptor,
        )
        adapter = DockerRuntimeAdapter("one")
        worker = Worker(
            store,
            adapter,
            "review",
            self.spec,
            self.console.provider,
            authorization_verifier=WorkAuthorizationVerifier(store, self.policy),
            poll_interval=0.05,
        )
        stop_requested, release_stop = asyncio.Event(), asyncio.Event()
        original = adapter.stop

        async def held_stop(*args, **kwargs):
            stop_requested.set()
            await release_stop.wait()
            return await original(*args, **kwargs)

        task = None
        try:
            with patch.object(adapter, "stop", side_effect=held_stop):
                task = asyncio.create_task(worker.once())
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    before = await store.get(run["id"])
                    if before["attempts"] and before["attempts"][0]["session_id"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    self.fail("native session did not start")
                attempt = before["attempts"][0]
                started = time.monotonic()
                await update(store, self.identities, "a", 1, {"enabled": False})
                await asyncio.wait_for(stop_requested.wait(), 10)
                requested_after = time.monotonic() - started
                pending = await store.get(run["id"])
                observed = await adapter.observe(attempt["id"])
                self.assertTrue(observed.alive)
                self.assertNotIn(pending["state"], ("failed", "cancelled", "succeeded"))
                self.assertEqual((await view(store, run["id"]))["state"], "revoked")
                release_stop.set()
                final = await asyncio.wait_for(task, 15)
                self.assertEqual(final["state"], "failed")
                self.assertEqual(final["attempt_id"], attempt["id"])
                self.assertEqual(
                    final["attempts"][0]["session_id"], attempt["session_id"]
                )
                self.assertFalse(final["attempts"][0]["remote"]["runtime"]["alive"])
                async with store.db.read("one") as c:
                    saved_artifacts = (
                        (
                            await c.execute(
                                select(artifacts.c.id).where(
                                    artifacts.c.run_id == run["id"]
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                self.assertTrue(saved_artifacts)
                OBSERVATIONS.append(
                    {
                        "run_id": run["id"],
                        "attempt_id": attempt["id"],
                        "session_id": attempt["session_id"],
                        "process_id": observed.pid,
                        "stop_requested_after_seconds": round(requested_after, 3),
                        "exit_confirmed_after_seconds": round(
                            time.monotonic() - started, 3
                        ),
                        "pending_stop_kept_nonterminal": True,
                        "confirmed_exit": True,
                        "history_preserved": True,
                        "artifact_ids": saved_artifacts,
                    }
                )
        finally:
            release_stop.set()
            if task and not task.done():
                task.cancel()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            saved = await store.get(run["id"])
            if saved.get("attempt_id"):
                await original(saved["attempt_id"], 0)
                await adapter.remove(saved["attempt_id"])
            adapter.client.close()
