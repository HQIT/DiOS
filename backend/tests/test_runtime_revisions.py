"""Immutable storage, scoped identity, restart, contention and recovery checks."""

import asyncio
import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import test_runtime_capacity as fixtures
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.revision_schema import metadata, receipts, revisions, versions
from app.runtime.revisions import DEPENDENCY_KINDS, Revisions, manifest
from app.runtime.store import RuntimeStore, digest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError


def fixture():
    dependencies = {key: [] for key in sorted(DEPENDENCY_KINDS)}
    dependencies["adapter"] = [
        {
            "name": "contract-fixture",
            "reference": "urn:dios:test:adapter",
            "digest": "sha256:" + digest("fixture-adapter-v1"),
            "version": "1",
            "visibility": "pinned",
        }
    ]
    dependencies["harness"] = [
        {
            "name": "remote-fixture",
            "reference": "https://example.invalid/a2a",
            "digest": None,
            "version": None,
            "visibility": "opaque",
        }
    ]
    dependencies["model"] = [
        {
            "name": "model-fixture",
            "reference": "urn:dios:test:model",
            "digest": None,
            "version": "model-v1",
            "visibility": "declared",
        }
    ]
    return {
        "schema_version": 1,
        "definition_id": "agent-fixture",
        "implementation_version": 1,
        "configuration": {
            "prompt": "只读检查，不执行代码。",
            "parameters": {"temperature": 0},
        },
        "dependencies": dependencies,
        "secret_refs": ["secret:provider-fixture"],
        "artifact_refs": [],
    }


class RevisionScenarios:
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    def manager(self, db=None, scope=None):
        return Revisions(db or self.db, scope or self.scope)

    async def test_restart_and_mutation_preserve_snapshot_and_digest(self):
        value = fixture()
        first = await self.manager().create("publish-1", value, actor="operator")
        identity = first["revision"]["id"]
        value["configuration"]["prompt"] = "new prompt"
        other = Database(self.url)
        try:
            saved = await self.manager(other).show(identity, include_manifest=True)
            self.assertEqual(
                saved["manifest"]["configuration"]["prompt"],
                fixture()["configuration"]["prompt"],
            )
            self.assertEqual(
                first["revision"]["content_digest"],
                "sha256:" + digest(saved["manifest"]),
            )
            self.assertNotIn("manifest", await self.manager(other).show(identity))
            second = await self.manager(other).create(
                "publish-2", value, actor="operator"
            )
            self.assertEqual(second["sequence"], 2)
            self.assertNotEqual(second["revision"]["id"], identity)
            self.assertEqual((await self.manager().show(identity)), first)
        finally:
            await other.close()

    async def test_independent_connections_deduplicate_and_allocate_order(self):
        databases = [Database(self.url) for _ in range(6)]
        try:
            duplicates = await asyncio.gather(
                *(
                    self.manager(db).create("same-key", fixture(), actor="operator")
                    for db in databases
                )
            )
            self.assertEqual(len({x["revision"]["id"] for x in duplicates}), 1)
            variants = []
            for i in range(len(databases)):
                value = fixture()
                value["configuration"]["variant"] = i
                variants.append(value)
            result = await asyncio.gather(
                *(
                    self.manager(db).create(f"variant-{i}", value, actor="operator")
                    for i, (db, value) in enumerate(zip(databases, variants))
                )
            )
            self.assertEqual({x["sequence"] for x in result}, set(range(2, 8)))
            alias = await self.manager().create(
                "other-key", fixture(), actor="operator"
            )
            self.assertEqual(alias, duplicates[0])
            with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
                await self.manager().create("same-key", variants[0], actor="operator")
            with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
                await self.manager().create(
                    "same-key", fixture(), actor="different-operator"
                )
        finally:
            for db in databases:
                await db.close()

    async def test_database_rejects_snapshot_and_receipt_mutation(self):
        row = await self.manager().create("one", fixture(), actor="operator")
        for table in (revisions, receipts):
            for operation in ("update", "delete"):
                with (
                    self.subTest(table=table.name, operation=operation),
                    self.assertRaises(DBAPIError),
                ):
                    async with self.db.transaction(self.scope) as c:
                        statement = (
                            table.delete()
                            if operation == "delete"
                            else table.update().values(created_at=0)
                        )
                        await c.execute(statement.where(table.c.scope == self.scope))
        self.assertEqual(await self.manager().show(row["revision"]["id"]), row)

    async def test_replace_and_upsert_cannot_mutate_snapshots_or_receipts(self):
        await self.manager().create("one", fixture(), actor="operator")
        for table in (revisions, receipts):
            async with self.db.read(self.scope) as c:
                row = dict(
                    (await c.execute(select(table).where(table.c.scope == self.scope)))
                    .mappings()
                    .one()
                )
            if self.postgres:
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            upsert = (
                insert(table)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=list(table.primary_key.columns),
                    set_={"created_at": 0},
                )
            )
            statements = [upsert]
            if not self.postgres:
                statements.append(
                    table.insert()
                    .prefix_with("OR REPLACE")
                    .values(**dict(row, created_at=0))
                )
            for statement in statements:
                with self.assertRaises(DBAPIError):
                    async with self.db.transaction(self.scope) as c:
                        await c.execute(statement)

    async def test_scope_isolation_and_bounded_pagination(self):
        one = await self.manager().create("one", fixture(), actor="operator")
        foreign = self.manager(scope=self.scope + "-other")
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await foreign.show(one["revision"]["id"])
        same = await foreign.create("one", fixture(), actor="operator")
        self.assertNotEqual(same["revision"]["id"], one["revision"]["id"])
        self.assertEqual(same["sequence"], 1)
        value = fixture()
        value["configuration"]["prompt"] = "second"
        await self.manager().create("two", value, actor="operator")
        page = await self.manager().list("agent-fixture", limit=1)
        self.assertEqual(page["next_after"], 1)
        final = await self.manager().list(
            "agent-fixture", after=page["next_after"], limit=1
        )
        self.assertEqual(final["items"][0]["sequence"], 2)
        self.assertIsNone(final["next_after"])
        self.assertEqual((await self.manager().list("absent"))["items"], [])
        for limit in (0, True, 101):
            with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_LIMIT"):
                await self.manager().list("agent-fixture", limit=limit)

    async def test_schema_migration_is_idempotent_and_unknown_version_fails_closed(
        self,
    ):
        self.assertFalse((await self.db.migrate())["changed"])
        self.assertEqual((await self.db.migrate())["revision_version"], 1)

        class Rollback(Exception):
            pass

        with self.assertRaises(Rollback):
            async with self.db.transaction(self.scope) as c:
                await c.execute(versions.update().values(version=2))
                from app.runtime.revision_schema import require

                with self.assertRaisesRegex(
                    RuntimeErrorCode, "REVISION_SCHEMA_INCOMPATIBLE"
                ):
                    await require(c)
                raise Rollback()

    async def test_manifest_rejection_has_no_persistent_receipt(self):
        value = fixture()
        value["configuration"]["apiKey"] = "private-fixture-value"
        with self.assertRaisesRegex(
            RuntimeErrorCode, "REVISION_SECRET_REFERENCE_REQUIRED"
        ):
            await self.manager().create("invalid", value, actor="operator")
        async with self.db.read(self.scope) as c:
            self.assertIsNone(
                (
                    await c.execute(
                        select(receipts).where(receipts.c.scope == self.scope)
                    )
                ).first()
            )


class SQLiteRevisionTests(RevisionScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_legacy_upgrade_preserves_runs_and_backup_restores_immutable_records(
        self,
    ):
        run = await self.store.submit("existing", {"fixture": "history"})
        async with self.db.transaction(self.scope) as c:
            await c.run_sync(metadata.drop_all)
        with self.assertRaisesRegex(RuntimeErrorCode, "REVISION_MIGRATION_REQUIRED"):
            await self.manager().list("agent-fixture")
        self.assertTrue((await self.db.migrate())["changed"])
        current = await self.store.get(run["id"])
        self.assertEqual({key: current[key] for key in run}, run)
        revision = await self.manager().create("one", fixture(), actor="operator")
        blobs = self.root / "blobs"
        blobs.mkdir()
        bundle = self.root / "backup"
        await asyncio.to_thread(backup_sqlite, self.url, str(blobs), str(bundle))
        info = await asyncio.to_thread(verify_backup, str(bundle))
        self.assertIn("runtime_revision_schema", str(info))
        restored = Database("sqlite+aiosqlite:///" + str(bundle / "state.sqlite"))
        try:
            self.assertEqual(
                await Revisions(restored, self.scope).show(revision["revision"]["id"]),
                revision,
            )
            current = await RuntimeStore(restored, self.scope).get(run["id"])
            self.assertEqual({key: current[key] for key in run}, run)
            with self.assertRaises(DBAPIError):
                async with restored.transaction(self.scope) as c:
                    await c.execute(revisions.delete())
        finally:
            await restored.close()

    async def test_missing_guard_rejected_and_corrupt_content_detected(self):
        revision = await self.manager().create("one", fixture(), actor="operator")
        async with self.db.transaction(self.scope) as c:
            await c.execute(text("DROP TRIGGER runtime_revisions_no_update"))
        with self.assertRaisesRegex(
            RuntimeErrorCode, "REVISION_IMMUTABILITY_GUARD_MISSING"
        ):
            await self.manager().show(revision["revision"]["id"])
        with self.assertRaisesRegex(
            RuntimeErrorCode, "REVISION_IMMUTABILITY_GUARD_MISSING"
        ):
            await self.db.migrate()
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                revisions.update().values(content_digest="sha256:" + "0" * 64)
            )
            await c.execute(
                text(
                    "CREATE TRIGGER runtime_revisions_no_update BEFORE UPDATE ON runtime_revisions BEGIN SELECT RAISE(ABORT, 'REVISION_IMMUTABLE'); END"
                )
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "REVISION_INTEGRITY_FAILED"):
            await self.manager().show(revision["revision"]["id"])

    async def test_cli_across_processes_preserves_identity_and_does_not_launch(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(fixture(), ensure_ascii=False))
        root = Path(__file__).resolve().parents[1]

        def cli(*args):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.runtime.cli",
                    "--database-url",
                    self.url,
                    "--scope",
                    self.scope,
                    "revision",
                    *args,
                ],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        first = await asyncio.to_thread(
            cli, "create", "--key", "cli", "--actor", "operator", "--file", str(path)
        )
        self.assertEqual(
            first,
            await asyncio.to_thread(
                cli,
                "create",
                "--key",
                "cli",
                "--actor",
                "operator",
                "--file",
                str(path),
            ),
        )
        shown = await asyncio.to_thread(
            cli, "show", first["revision"]["id"], "--manifest"
        )
        self.assertEqual(shown["manifest"], fixture())
        self.assertEqual(
            (await asyncio.to_thread(cli, "list", "agent-fixture"))["items"], [first]
        )
        from app.runtime.schema import runs, workers

        async with self.db.read(self.scope) as c:
            for table in (runs, workers):
                self.assertIsNone(
                    (
                        await c.execute(
                            select(table).where(table.c.scope == self.scope)
                        )
                    ).first()
                )


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "isolated PostgreSQL required"
)
class PostgreSQLRevisionTests(RevisionScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class ManifestTests(unittest.TestCase):
    def test_published_manifest_schema_accepts_fixture_and_rejects_mutable_adapter(
        self,
    ):
        from jsonschema import Draft202012Validator

        root = Path(__file__).resolve().parents[2]
        schema = json.loads(
            (root / "contracts/v1/revision-manifest.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(fixture())
        bad = fixture()
        bad["dependencies"]["adapter"][0].update(visibility="declared", digest=None)
        self.assertFalse(validator.is_valid(bad))

    def test_bad_shapes_unpinned_controlled_dependencies_and_inline_credentials(self):
        cases = []
        for key, replacement in [
            ("schema_version", True),
            ("implementation_version", 0),
            ("definition_id", "bad/id"),
            ("secret_refs", ["raw secret value"]),
            ("artifact_refs", ["a", "a"]),
            ("configuration", {"x": float("nan")}),
        ]:
            value = fixture()
            value[key] = replacement
            cases.append(value)
        for key in (
            "api_key",
            "apiKey",
            "OPENAI_API_KEY",
            "password",
            "authorization",
            "access_token",
            "secrets",
        ):
            value = fixture()
            value["configuration"][key] = "private-value"
            cases.append(value)
        for url in (
            "https://user:password@example.invalid",
            "https://example.invalid/?api_key=private",
        ):
            value = fixture()
            value["dependencies"]["harness"][0]["reference"] = url
            cases.append(value)
        value = fixture()
        del value["dependencies"]["skill"]
        cases.append(value)
        value = fixture()
        value["dependencies"]["adapter"][0]["visibility"] = "opaque"
        cases.append(value)
        value = fixture()
        value["dependencies"]["adapter"][0]["digest"] = None
        cases.append(value)
        value = fixture()
        value["dependencies"]["adapter"] *= 2
        cases.append(value)
        value = fixture()
        value["configuration"]["blob"] = "x" * 262144
        cases.append(value)
        value = fixture()
        value["configuration"]["key"] = "-----BEGIN PRIVATE KEY-----"
        cases.append(value)
        for i, value in enumerate(cases):
            with self.subTest(index=i), self.assertRaises(RuntimeErrorCode):
                manifest(value)

    def test_snapshots_own_nested_input_and_accept_public_budget_parameters(self):
        value = fixture()
        value["configuration"]["token_budget"] = 100
        snapshot = manifest(value)
        value["configuration"]["token_budget"] = 200
        self.assertEqual(snapshot["configuration"]["token_budget"], 100)
        self.assertEqual(snapshot, manifest(copy.deepcopy(snapshot)))


if __name__ == "__main__":
    unittest.main()
