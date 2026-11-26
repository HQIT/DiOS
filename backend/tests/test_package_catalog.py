"""API/CLI catalog parity, admission evidence, pagination and access boundaries."""

import asyncio
import hashlib
import json
import subprocess
import sys
import unittest

import test_runtime_task_console as fixtures
from app.api.os.runtime_tasks import service
from app.runtime.database import RuntimeErrorCode
from app.runtime.execution_revisions import compile_profile
from app.runtime.object_lifetime import ObjectLifetime
from app.runtime.package_admission import PackageAdmission
from app.runtime.package_catalog import PackageCatalog
from app.runtime.package_lock import resolve
from app.runtime.package_payloads import tree_descriptor
from app.runtime.package_registry import PackageRegistry
from app.runtime.revisions import Revisions
from package_admission_fixtures import admit_fixture, declared, review_fixture
from test_execution_revisions import profile
from test_package_lock import package, requirement


class PackageCatalogTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = fixtures.TaskConsoleTests.asyncTearDown

    async def seed(self):
        self.registry = PackageRegistry(self.db, "one")
        self.catalog = PackageCatalog(self.db, "one")
        self.blobs = self.root / "blobs"
        self.blobs.mkdir()
        (self.blobs / hashlib.sha256(b"original").hexdigest()).write_bytes(b"original")
        self.entries = []
        for version in ("1", "2", "3"):
            value = declared(package(version=version))
            if version == "3":
                value["kind"] = "skill"
            row = await self.registry.register(
                value, self.blobs, self.console.provider, key=version, actor="publisher"
            )
            self.entries.append(row)
        await admit_fixture(self.db, "one", self.root, self.entries[0]["manifest"])
        statement = await review_fixture(
            self.db, "one", self.root, self.entries[2]["manifest"]
        )
        await PackageAdmission(self.db, "one").review(
            self.entries[2]["manifest"],
            "rejected",
            statement,
            expected_version=0,
            key="reject",
            actor="reviewer",
        )

    async def cli(self, *args):
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "app.runtime.cli",
                "--database-url",
                "sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite"),
                "--scope",
                "one",
                "registry",
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, json.loads(result.stdout)

    async def test_api_cli_and_core_share_filters_and_values(self):
        await self.seed()
        for filters in (
            {},
            {"q": "AG", "kind": "harness"},
            {"version": "1.0.0"},
            {"admission": "pending"},
            {"admission": "rejected"},
            {"q": "%"},
            {"kind": "tool"},
        ):
            expected = await self.catalog.list(**filters)
            response = await self.client.get(
                "/api/os/runtime/packages", params=filters, headers=self.headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), expected)
            self.assertEqual(response.headers["cache-control"], "no-store")
            args = [
                piece
                for key, value in filters.items()
                for piece in ("--query" if key == "q" else "--" + key, value)
            ]
            status, body = await self.cli("catalog", *args)
            self.assertEqual((status, body), (0, expected))
        ref = self.entries[0]["digest"]
        response = await self.client.get(
            "/api/os/runtime/packages/" + ref, headers=self.headers
        )
        self.assertEqual(response.json(), await self.catalog.detail(ref))
        self.assertNotIn("storage", response.text)
        self.assertNotIn(str(self.root), response.text)

    async def test_filter_errors_pagination_and_revocation_are_current(self):
        await self.seed()
        seen = []
        after = ""
        while True:
            page = await self.catalog.list(limit=1, after=after)
            seen.extend(x["digest"] for x in page["items"])
            if not page["next_after"]:
                break
            after = page["next_after"]
        self.assertEqual(sorted(seen), sorted(x["digest"] for x in self.entries))
        for field, value in (
            ("kind", "other"),
            ("version", "latest"),
            ("admission", "safe"),
            ("after", "bad"),
            ("limit", "0"),
        ):
            response = await self.client.get(
                "/api/os/runtime/packages", params={field: value}, headers=self.headers
            )
            self.assertEqual(
                (response.status_code, response.json()["detail"]),
                (422, "INVALID_PACKAGE_FILTER"),
            )
            self.assertEqual(
                await self.cli("catalog", "--" + field, value),
                (2, {"error": "INVALID_PACKAGE_FILTER"}),
            )
        first = self.entries[0]
        current = (await PackageAdmission(self.db, "one").show(first["digest"]))[
            "current"
        ]
        await PackageAdmission(self.db, "one").review(
            first["manifest"],
            "revoked",
            current["statement"],
            expected_version=1,
            key="revoke",
            actor="reviewer",
        )
        self.assertEqual((await self.catalog.list(admission="admitted"))["items"], [])
        self.assertEqual(
            (await self.catalog.list(admission="revoked"))["items"][0]["admission"][
                "version"
            ],
            2,
        )

    async def test_invalid_evidence_does_not_appear_as_effectively_admitted(self):
        await self.seed()
        current = (
            await PackageAdmission(self.db, "one").show(self.entries[0]["digest"])
        )["current"]
        ref = current["statement"]["predicate"]["checks"]["source"]["evidence"]
        await ObjectLifetime(self.db, "one").change(
            ref,
            expected_revision=0,
            key="delete-evidence",
            actor="operator",
            reason="fixture",
            delete=True,
        )
        row = (await self.catalog.list(admission="admitted"))["items"][0]
        self.assertFalse(row["admission"]["evidence_valid"])
        self.assertIn("PACKAGE_EVIDENCE_UNAVAILABLE", row["limitations"])

    async def test_catalog_requires_operator_and_scope_isolation(self):
        await self.seed()
        self.assertEqual(
            (await self.client.get("/api/os/runtime/packages")).status_code, 401
        )
        self.app.dependency_overrides[service] = lambda: self.console.for_principal(
            {"id": "application"}
        )
        try:
            self.assertEqual(
                (
                    await self.client.get(
                        "/api/os/runtime/packages", headers=self.headers
                    )
                ).status_code,
                403,
            )
        finally:
            self.app.dependency_overrides.clear()
        self.assertEqual((await PackageCatalog(self.db, "other").list())["items"], [])
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await PackageCatalog(self.db, "other").detail(self.entries[0]["digest"])
        missing = await self.client.get(
            "/api/os/runtime/packages/sha256:" + "f" * 64, headers=self.headers
        )
        self.assertEqual(
            (missing.status_code, missing.json()["detail"]), (404, "NOT_FOUND")
        )

    async def test_related_revisions_are_exact_and_keep_bounded_continuation(self):
        await self.seed()
        row = self.entries[0]
        lock = resolve([row["manifest"]], [requirement("agent")])
        raw = profile(mode="e2ag")
        raw["package_lock"] = lock
        raw["packaged_mounts"] = [tree_descriptor(lock)]
        saved = await Revisions(self.db, "one").create(
            "locked", compile_profile(raw), actor="operator"
        )
        await Revisions(self.db, "one").create(
            "other", compile_profile(profile()), actor="operator"
        )
        after = ""
        found = []
        while True:
            page = await self.catalog.releases(row["digest"], after=after, limit=1)
            found += page["items"]
            if not page["next_after"]:
                break
            after = page["next_after"]
        self.assertEqual([item["id"] for item in found], [saved["revision"]["id"]])
        response = await self.client.get(
            "/api/os/runtime/packages/" + row["digest"] + "/releases",
            headers=self.headers,
        )
        self.assertEqual(response.json(), await self.catalog.releases(row["digest"]))
