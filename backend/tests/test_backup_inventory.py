"""Backup closure checks against committed records, concurrent writes and file drift."""

import asyncio
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_runtime_recovery as fixtures
from app.runtime.backup_inventory import inventory
from app.runtime.database import RuntimeErrorCode
from app.runtime.execution_revisions import compile_profile, load_profile
from app.runtime.recovery import backup_sqlite, restore_sqlite, verify_backup
from app.runtime.revisions import Revisions
from app.runtime.schema import runs
from app.runtime.store import digest


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RecoveryTests.asyncSetUp
    asyncTearDown = fixtures.RecoveryTests.asyncTearDown
    seed = fixtures.RecoveryTests.seed

    async def versioned(self, *, workspace=False):
        raw = {
            "id": "fixed",
            "bridge": "jsonl-v1",
            "governance": {"mode": "trusted-local"},
            "launch": {"image": "sha256:" + "1" * 64, "command": ["true"]},
        }
        if workspace:
            raw["launch"]["mounts"] = [
                {"source": str(self.root), "target": "/input", "read_only": True}
            ]
            raw["input_mount_targets"] = ["/input"]
        row = await Revisions(self.db, "test").create(
            "revision", compile_profile(raw), actor="test"
        )
        ref = row["revision"]["id"]
        _, profile, spec, _, _ = await load_profile(self.db, "test", ref)
        return await self.store.submit(
            "fixed",
            {
                "runtime_profile": profile,
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
            },
            revision_ref=ref,
        )

    async def test_named_configuration_revision_and_external_inventory_survive_restore(
        self,
    ):
        await self.seed()
        run = await self.versioned(workspace=True)
        config = self.root / "profile.json"
        config.write_text('{"说明":"配置随版本恢复"}')
        result = await asyncio.to_thread(
            backup_sqlite,
            self.url,
            self.provider.root,
            self.bundle,
            config_files=["profile=" + str(config)],
        )
        self.assertEqual(result["format"], 3)
        self.assertEqual(
            result["inventory"]["references"]["runtime_runs.input.revision_ref"], 1
        )
        self.assertEqual(result["inventory"]["tables"]["runtime_revisions"]["rows"], 1)
        self.assertTrue(
            any(e["kind"] == "image" for e in result["inventory"]["external"])
        )
        self.assertTrue(
            any(e["kind"] == "workspace" for e in result["inventory"]["external"])
        )
        self.assertTrue(
            any(e["kind"] == "execution" for e in result["inventory"]["external"])
        )
        self.assertEqual(verify_backup(self.bundle), result)
        receipt = await restore_sqlite(self.bundle, self.restored)
        self.assertEqual(
            (self.restored / "configuration/profile").read_bytes(), config.read_bytes()
        )
        self.assertEqual(receipt["external_recovery"], result["inventory"]["external"])
        self.assertEqual((await self.store.get(run["id"]))["state"], "queued")

    async def test_concurrent_commit_after_snapshot_does_not_enter_inventory(self):
        await self.seed()
        copied = threading.Event()
        proceed = threading.Event()

        def held(path, budget):
            copied.set()
            if not proceed.wait(10):
                raise TimeoutError("writer did not finish")
            return inventory(path, budget)

        with patch("app.runtime.backup_inventory.inventory", side_effect=held):
            task = asyncio.create_task(
                asyncio.to_thread(
                    backup_sqlite, self.url, self.provider.root, self.bundle
                )
            )
            try:
                self.assertTrue(await asyncio.to_thread(copied.wait, 10))
                late = await self.store.submit("after-snapshot", {"valid": "并发写入"})
            finally:
                proceed.set()
            result = await task
        self.assertEqual(result["inventory"]["tables"]["runtime_runs"]["rows"], 1)
        with sqlite3.connect(self.bundle / "state.sqlite") as db:
            self.assertIsNone(
                db.execute(
                    "SELECT id FROM runtime_runs WHERE id=?", (late["id"],)
                ).fetchone()
            )
        self.assertEqual(verify_backup(self.bundle), result)

    async def test_configuration_changed_during_snapshot_prevents_publication(self):
        config = self.root / "policy.json"
        config.write_text('{"version":1}')

        def changed(path, budget):
            config.write_text('{"version":2}')
            return inventory(path, budget)

        with (
            patch("app.runtime.backup_inventory.inventory", side_effect=changed),
            self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_CONFIGURATION_CHANGED"),
        ):
            await asyncio.to_thread(
                backup_sqlite,
                self.url,
                self.provider.root,
                self.bundle,
                config_files=["policy=" + str(config)],
            )
        self.assertFalse((self.bundle / "manifest.json").exists())

    async def test_json_reference_missing_and_cross_scope_are_rejected(self):
        run = await self.versioned()
        value = dict(run["input"], revision_ref="rev-missing")
        async with self.db.transaction("test") as c:
            await c.execute(
                runs.update()
                .where(runs.c.id == run["id"])
                .values(input=value, input_digest=digest(value))
            )
        with self.assertRaisesRegex(
            RuntimeErrorCode, "BACKUP_REFERENCE_INVALID:runtime_runs.input.revision_ref"
        ):
            await asyncio.to_thread(
                backup_sqlite, self.url, self.provider.root, self.bundle
            )
        self.assertFalse((self.bundle / "manifest.json").exists())
        async with self.db.transaction("test") as c:
            await c.execute(
                runs.update()
                .where(runs.c.id == run["id"])
                .values(input=run["input"], scope="different")
            )
        with self.assertRaisesRegex(
            RuntimeErrorCode, "BACKUP_REFERENCE_INVALID:runtime_runs.input.revision_ref"
        ):
            await asyncio.to_thread(
                backup_sqlite, self.url, self.provider.root, self.root / "other"
            )

    async def test_configuration_corruption_missing_and_inventory_tampering_fail(self):
        config = self.root / "policy.json"
        config.write_text('{"version":1}')
        await asyncio.to_thread(
            backup_sqlite,
            self.url,
            self.provider.root,
            self.bundle,
            config_files=["policy=" + str(config)],
        )
        saved = self.bundle / "configuration/policy"
        raw = saved.read_bytes()
        saved.write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_CONFIGURATION_MISMATCH"):
            verify_backup(self.bundle)
        saved.unlink()
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_FILE_MISSING"):
            verify_backup(self.bundle)
        saved.write_bytes(raw)
        path = self.bundle / "manifest.json"
        value = json.loads(path.read_text())
        value["inventory"]["external"].append({"kind": "invented"})
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_INVENTORY_MISMATCH"):
            verify_backup(self.bundle)

    async def test_legacy_format_one_is_still_readable(self):
        await self.seed()
        result = await asyncio.to_thread(
            backup_sqlite, self.url, self.provider.root, self.bundle
        )
        result["format"] = 1
        del result["inventory"]
        del result["configuration"]
        (self.bundle / "manifest.json").write_text(json.dumps(result))
        self.assertEqual(verify_backup(self.bundle), result)
        self.assertEqual(
            (await restore_sqlite(self.bundle, self.restored))["mode"], "restore_hold"
        )

    async def test_configuration_names_and_symlinks_rejected_before_destination(self):
        config = self.root / "policy.json"
        config.write_text("{}")
        link = self.root / "link"
        link.symlink_to(config)
        for values in (
            ["../escape=" + str(config)],
            ["policy=" + str(config)] * 2,
            ["policy=" + str(link)],
        ):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(
                    RuntimeErrorCode, "BACKUP_CONFIGURATION_INVALID"
                ),
            ):
                await asyncio.to_thread(
                    backup_sqlite,
                    self.url,
                    self.provider.root,
                    self.bundle,
                    config_files=values,
                )
            self.assertFalse(self.bundle.exists())


if __name__ == "__main__":
    unittest.main()
