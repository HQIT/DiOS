"""Offline typed copy of a verified SQLite bundle into an empty PostgreSQL schema."""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import MetaData, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from .database import RuntimeErrorCode
from .lifecycle import Lifecycle
from .recovery import _chunks, _file_info, _sync_dir, _write, verify_backup
from .storage_lease import StorageLease, artifacts_lock
from .store import encoded


def row_digest(rows):
    """Order-independent table digest, preserving duplicate rows and JSON types."""
    values = sorted(hashlib.sha256(encoded(dict(row))).digest() for row in rows)
    return {"rows": len(values), "sha256": hashlib.sha256(b"".join(values)).hexdigest()}


def held_rows(name, rows, *, source):
    """The two explicit recovery deltas; all other fields remain byte-logical equal."""
    values = [dict(row) for row in rows]
    if name == "runtime_lifecycle":
        global_row = next((r for r in values if r["key"] == "global"), None)
        if source:
            if global_row is None:
                global_row = {
                    "key": "global",
                    "scope": None,
                    "mode": "restore_hold",
                    "revision": 1,
                    "updated_at": 0,
                }
                values.append(global_row)
            elif global_row["mode"] != "restore_hold":
                global_row.update(
                    mode="restore_hold", revision=global_row["revision"] + 1
                )
        elif global_row is None or global_row["mode"] != "restore_hold":
            raise RuntimeErrorCode("TRANSFER_HOLD_MISSING")
        if not math.isfinite(global_row["updated_at"]) or global_row["updated_at"] < 0:
            raise RuntimeErrorCode("TRANSFER_HOLD_INVALID")
        global_row["updated_at"] = 0
    if name == "runtime_worker_credentials":
        for row in values:
            if source and row["state"] == "active":
                row.update(
                    state="revoked",
                    version=row["version"] + 1,
                    previous_until=None,
                    updated_at=0,
                )
    return values


def comparable(name, source_rows, target_rows):
    expected = held_rows(name, source_rows, source=True)
    actual = held_rows(name, target_rows, source=False)
    if name == "runtime_worker_credentials":
        revoked = {r["id"] for r in source_rows if r["state"] == "active"}
        for row in actual:
            if row["id"] in revoked:
                if not math.isfinite(row["updated_at"]) or row["updated_at"] < 0:
                    raise RuntimeErrorCode("TRANSFER_CREDENTIAL_INVALID")
                row["updated_at"] = 0
    return row_digest(expected), row_digest(actual)


def copy_file(source, target, expected):
    if target.exists() or target.is_symlink():
        if _file_info(target) != expected:
            raise RuntimeErrorCode("TRANSFER_FILE_CONFLICT")
        return
    temporary = target.with_name("." + target.name + "." + uuid4().hex + ".pending")
    try:
        if _write(temporary, _chunks(source)) != expected:
            raise RuntimeErrorCode("TRANSFER_FILE_CONFLICT")
        os.link(temporary, target)
        _sync_dir(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def copy_bundle_files(bundle, destination, manifest):
    objects = dict(manifest["objects"])
    objects.update(manifest.get("tool_receipts", {}).get("objects", {}))
    for key, expected in objects.items():
        copy_file(bundle / "objects" / key, destination / "objects" / key, expected)
    if "tool_receipts" in manifest:
        from .tool_receipt_backup import install

        install(
            bundle / "tool-receipts",
            SimpleNamespace(root=destination / "objects"),
            manifest["tool_receipts"],
        )
        receipt_root = destination / "objects" / ".tool-receipts-v1"
        if receipt_root.is_dir():
            _sync_dir(receipt_root)
    config = destination / "configuration"
    config.mkdir(mode=0o700, exist_ok=True)
    if config.is_symlink():
        raise RuntimeErrorCode("TRANSFER_PATH_INVALID")
    for name, expected in manifest.get("configuration", {}).items():
        copy_file(bundle / "configuration" / name, config / name, expected)
    for directory in (destination / "objects", config, destination, destination.parent):
        _sync_dir(directory)
    return len(objects)


def destination_lease(destination, source_digest):
    marker = destination / "transfer-source.json"
    if not destination.exists():
        destination.mkdir(mode=0o700)
        _write(marker, [encoded({"source_sha256": source_digest})])
    if destination.is_symlink() or not marker.is_file() or marker.is_symlink():
        raise RuntimeErrorCode("TRANSFER_DESTINATION_NOT_OWNED")
    if marker.stat().st_size > 1024 or json.loads(marker.read_text()) != {
        "source_sha256": source_digest
    }:
        raise RuntimeErrorCode("TRANSFER_SOURCE_CHANGED")
    objects = destination / "objects"
    objects.mkdir(mode=0o700, exist_ok=True)
    if objects.is_symlink():
        raise RuntimeErrorCode("TRANSFER_PATH_INVALID")
    return StorageLease(artifacts_lock(objects), exclusive=True)


async def reflect(connection):
    metadata = MetaData()
    names = await connection.run_sync(lambda c: inspect(c).get_table_names())
    if any(not name.startswith("runtime_") for name in names):
        raise RuntimeErrorCode("TRANSFER_DEDICATED_DATABASE_REQUIRED")
    await connection.run_sync(lambda c: metadata.reflect(bind=c))
    return metadata


async def rows(connection, table, limit):
    # A bounded batch insert follows; rejecting an oversized transfer never drops rows.
    result = await connection.execute(select(table).limit(limit + 1))
    values = [dict(row) for row in result.mappings()]
    if len(values) > limit:
        raise RuntimeErrorCode("TRANSFER_ROW_LIMIT")
    return values


async def transfer(bundle, destination, target, *, apply=False, row_limit=100000):
    """Only already-migrated, isolated targets; never starts or resumes execution."""
    if target.engine.dialect.name != "postgresql":
        raise RuntimeErrorCode("TRANSFER_POSTGRESQL_REQUIRED")
    if type(row_limit) is not int or not 1 <= row_limit <= 1000000:
        raise RuntimeErrorCode("TRANSFER_ROW_LIMIT")
    bundle, destination = Path(bundle).absolute(), Path(destination).absolute()
    manifest = await asyncio.to_thread(verify_backup, bundle)
    source_digest = manifest["database"]["sha256"]
    source = create_async_engine(
        "sqlite+aiosqlite:///"
        + (bundle / "state.sqlite").as_uri()
        + "?mode=ro&uri=true"
    )
    lease = None
    try:
        if apply:
            lease = await asyncio.to_thread(
                destination_lease, destination, source_digest
            )
            object_count = await asyncio.to_thread(
                copy_bundle_files, bundle, destination, manifest
            )
        else:
            object_count = len(
                set(manifest["objects"])
                | set(manifest.get("tool_receipts", {}).get("objects", {}))
            )
        async with source.connect() as incoming, target.engine.begin() as outgoing:
            source_meta, target_meta = await reflect(incoming), await reflect(outgoing)
            if set(source_meta.tables) != set(target_meta.tables):
                raise RuntimeErrorCode("TRANSFER_SCHEMA_MISMATCH")
            for name, table in source_meta.tables.items():
                other = target_meta.tables[name]
                if set(table.columns.keys()) != set(other.columns.keys()):
                    raise RuntimeErrorCode("TRANSFER_SCHEMA_MISMATCH")
                if [c.name for c in table.primary_key] != [
                    c.name for c in other.primary_key
                ]:
                    raise RuntimeErrorCode("TRANSFER_SCHEMA_MISMATCH")
                if any(
                    column.type._type_affinity
                    != other.c[column.name].type._type_affinity
                    or column.nullable != other.c[column.name].nullable
                    for column in table.c
                ):
                    raise RuntimeErrorCode("TRANSFER_SCHEMA_MISMATCH")
            await outgoing.execute(text("SET LOCAL lock_timeout='5s'"))
            # Block all competing writers, including ones not using Runtime advisory locks.
            identifiers = ",".join(
                outgoing.dialect.identifier_preparer.quote(name)
                for name in sorted(target_meta.tables)
            )
            await outgoing.execute(
                text(
                    "LOCK TABLE "
                    + identifiers
                    + (" IN ACCESS EXCLUSIVE MODE" if apply else " IN SHARE MODE")
                )
            )
            source_rows, target_rows = {}, {}
            for name in source_meta.tables:
                source_rows[name] = await rows(
                    incoming, source_meta.tables[name], row_limit
                )
                target_rows[name] = await rows(
                    outgoing, target_meta.tables[name], row_limit
                )
            schema_names = set(manifest["schemas"])
            for name in schema_names:
                if row_digest(source_rows[name]) != row_digest(target_rows[name]):
                    raise RuntimeErrorCode("TRANSFER_SCHEMA_MISMATCH")
            from . import (
                package_admission_schema,
                package_registry_schema,
                revision_schema,
                studio_release_schema,
                technical_release_schema,
            )

            for component in (
                revision_schema,
                package_registry_schema,
                package_admission_schema,
                studio_release_schema,
                technical_release_schema,
            ):
                if not await component.check(outgoing):
                    raise RuntimeErrorCode("TRANSFER_SCHEMA_MISMATCH")
            empty = not any(
                value for name, value in target_rows.items() if name not in schema_names
            )
            if not empty:
                for name, values in source_rows.items():
                    expected, actual = comparable(name, values, target_rows[name])
                    if expected != actual:
                        raise RuntimeErrorCode("TRANSFER_TARGET_CONFLICT:" + name)
            if not apply:
                return {
                    "state": "ready" if empty else "already_transferred",
                    "tables": len(source_rows),
                    "rows": sum(map(len, source_rows.values())),
                    "objects": object_count,
                    "source_sha256": source_digest,
                    "execution": "not_started",
                }
            if empty:
                for table in target_meta.sorted_tables:
                    if table.name in schema_names:
                        continue
                    values = source_rows[table.name]
                    for offset in range(0, len(values), 200):
                        await outgoing.execute(
                            table.insert(), values[offset : offset + 200]
                        )
                await Lifecycle(target, "local").hold_restored_in_transaction(outgoing)
            checks = {}
            for name, values in source_rows.items():
                actual_rows = await rows(outgoing, target_meta.tables[name], row_limit)
                expected, actual = comparable(name, values, actual_rows)
                if expected != actual:
                    raise RuntimeErrorCode("TRANSFER_VERIFY_FAILED:" + name)
                checks[name] = {
                    "source": row_digest(values),
                    "target": row_digest(actual_rows),
                    "normalized": actual,
                    "matched": True,
                }
            if (
                await asyncio.to_thread(_file_info, bundle / "state.sqlite")
                != manifest["database"]
            ):
                raise RuntimeErrorCode("TRANSFER_SOURCE_CHANGED")
        report = {
            "format": 1,
            "state": "transferred" if empty else "already_transferred",
            "mode": "restore_hold",
            "source_sha256": source_digest,
            "objects": object_count,
            "tables": checks,
            "external_recovery": manifest.get("inventory", {}).get("external", []),
            "mutations": ["global restore_hold", "active Worker credentials revoked"],
            "execution": "not_started",
        }
        receipt = destination / ("transfer-" + uuid4().hex + ".json")
        await asyncio.to_thread(_write, receipt, [encoded(report)])
        await asyncio.to_thread(_sync_dir, destination)
        return report
    finally:
        await source.dispose()
        if lease:
            lease.close()
