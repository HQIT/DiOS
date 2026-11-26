"""Observation failures never bypass authority, ownership or execution deadlines."""

import asyncio
import json
import unittest
from unittest.mock import patch

import requests
from app.runtime.database import RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter
from docker.errors import APIError
from test_runtime_work_authorizations import WorkAuthorizationTests
from test_runtime_worker import FakeAdapter


class ObservationAuthorityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = WorkAuthorizationTests.asyncSetUp
    asyncTearDown = WorkAuthorizationTests.asyncTearDown
    submit = WorkAuthorizationTests.submit
    worker = WorkAuthorizationTests.worker

    async def exercise(self, change, expected, timeout=30):
        run = await self.submit()
        adapter = FakeAdapter(failure=True)
        worker = self.worker(adapter)
        worker.timeout = timeout
        task = asyncio.create_task(worker.once())
        try:
            async with asyncio.timeout(3):
                while not adapter.running:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            await change(run)
            result = await asyncio.wait_for(task, 8)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["attempts"][0]["error"]["code"], expected)
            self.assertEqual(adapter.started, 1)
            self.assertTrue(adapter.stopped)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_policy_change_revalidated_during_query_outage(self):
        async def change(run):
            config = json.loads(self.policy.path.read_text())
            config["subject"] = "operator:changed"
            self.policy.path.write_text(json.dumps(config))

        await self.exercise(change, "WORK_POLICY_CHANGED")

    async def test_revoke_stops_despite_query_outage(self):
        async def revoke(run):
            current = await self.store.get(run["id"])
            await self.store.revoke(run["id"], current["version"])

        await self.exercise(revoke, "AUTH_REVOKED")

    async def test_process_deadline_still_enforced(self):
        async def unchanged(run):
            pass

        await self.exercise(unchanged, "PROCESS_DEADLINE_EXCEEDED", timeout=0.5)


class DockerObservationClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_read_transport_failures_are_retryable(self):
        from unittest.mock import Mock

        adapter = DockerRuntimeAdapter("test", client=Mock())
        for error in (
            requests.exceptions.ReadTimeout(),
            requests.exceptions.ConnectionError(),
            APIError("busy", response=Mock(status_code=503)),
            APIError("limit", response=Mock(status_code=429)),
        ):
            with (
                patch.object(adapter, "_get", side_effect=error),
                self.assertRaisesRegex(
                    RuntimeErrorCode, "RUNTIME_OBSERVATION_UNAVAILABLE"
                ),
            ):
                await adapter.observe("task")
        for error in (
            RuntimeErrorCode("RUNTIME_SCOPE_MISMATCH"),
            APIError("denied", response=Mock(status_code=403)),
        ):
            with patch.object(adapter, "_get", side_effect=error):
                with self.assertRaises(type(error)) as raised:
                    await adapter.observe("task")
                self.assertIs(raised.exception, error)
