"""Scrape credentials confer no business, operator or execution permissions."""

import os
import unittest
from unittest.mock import patch
from app.api.os import runtime_tasks
from app.runtime.monitor import snapshot
import test_runtime_principals as fixtures


class MonitorAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PrincipalTests.asyncSetUp
    asyncTearDown = fixtures.PrincipalTests.asyncTearDown
    save = fixtures.PrincipalTests.save
    headers_for = fixtures.PrincipalTests.headers_for

    async def configured(self):
        path = self.root / "monitor.token"
        path.write_text("a" * 43)
        path.chmod(0o600)
        return path, {"Authorization": "Bearer " + "a" * 43}

    async def test_scrape_only_and_operator_unchanged(self):
        path, headers = await self.configured()
        with patch.dict(os.environ, {"DIOS_RUNTIME_MONITOR_TOKEN_FILE": str(path)}):
            value = await self.client.get(
                "/api/os/runtime/scheduler-metrics", headers=headers
            )
            self.assertEqual(value.status_code, 200, value.text)
            self.assertIn("dios_scheduler_state", value.text)
            self.assertNotIn("a" * 43, value.text)
            for method, endpoint, body in [
                ("GET", "runs", None),
                ("GET", "scheduler-health", None),
                ("GET", "profiles", None),
                ("POST", "runs", {}),
                ("PUT", "capacity", {}),
                ("POST", "runs/absent/cancel", {}),
                ("POST", "runs/absent/tools/absent/dispatch", {}),
            ]:
                r = await self.client.request(
                    method, "/api/os/runtime/" + endpoint, headers=headers, json=body
                )
                self.assertIn(
                    r.status_code,
                    (401, 403),
                    (endpoint, r.status_code, r.text),
                )
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/scheduler-metrics", headers=self.headers
                    )
                ).status_code,
                200,
            )
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/scheduler-metrics",
                        headers=self.headers_for("a"),
                    )
                ).status_code,
                403,
            )

    async def test_rotation_revocation_permissions_symlink_and_missing_file(self):
        path, headers = await self.configured()
        with patch.dict(os.environ, {"DIOS_RUNTIME_MONITOR_TOKEN_FILE": str(path)}):
            path.write_text("b" * 43)
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/scheduler-metrics", headers=headers
                    )
                ).status_code,
                401,
            )
            fresh = {"Authorization": "Bearer " + "b" * 43}
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/scheduler-metrics", headers=fresh
                    )
                ).status_code,
                200,
            )
            path.chmod(0o644)
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/scheduler-metrics", headers=fresh
                    )
                ).status_code,
                503,
            )
            path.unlink()
            path.symlink_to(self.root / "absent")
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/scheduler-metrics", headers=fresh
                    )
                ).status_code,
                503,
            )
            path.unlink()
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/scheduler-metrics", headers=fresh
                    )
                ).status_code,
                503,
            )

    async def test_rotation_while_reading_drops_response(self):
        path, headers = await self.configured()

        async def rotate(*args):
            value = await snapshot(*args)
            path.write_text("c" * 43)
            return value

        with (
            patch.dict(os.environ, {"DIOS_RUNTIME_MONITOR_TOKEN_FILE": str(path)}),
            patch("app.runtime.monitor.snapshot", rotate),
        ):
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/scheduler-metrics", headers=headers
                    )
                ).status_code,
                401,
            )

    async def test_operator_token_cannot_be_repurposed_as_monitor_secret(self):
        from app.services.monitor_access import accepts
        from fastapi import HTTPException

        path, headers = await self.configured()
        with (
            patch.dict(os.environ, {"DIOS_RUNTIME_MONITOR_TOKEN_FILE": str(path)}),
            self.assertRaises(HTTPException) as error,
        ):
            accepts("a" * 43, "a" * 43)
        self.assertEqual(error.exception.status_code, 503)
