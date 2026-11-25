"""Read-only A2A history export and explicit, idempotent archive import."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.history_archive import import_group, validate_group
from app.runtime.store import encoded, identifier
from app.runtime.tool_receipts import durable_io

FORMAT = "dios-managed-history-v1"
TERMINAL = {"completed", "failed", "canceled"}
MAX_LINE = 1024 * 1024
MAX_INDEX = 128 * 1024 * 1024


def _private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _blob(archive, value):
    blob = archive.put([encoded(value)])
    return {"digest": blob.digest, "size": blob.size}


def _record(archive, kind, source_id, table, raw, metadata):
    ref = _blob(archive, raw)
    return {
        "kind": kind,
        "source_id": source_id,
        "record": {
            "adapter": FORMAT,
            "source_table": table,
            "source_record": ref,
            **metadata,
        },
        "blobs": [ref],
    }


def _link(task_id, relation, kind, target_id):
    return {
        "kind": "task",
        "source_id": task_id,
        "relation": relation,
        "target_kind": kind,
        "target_source_id": target_id,
    }


def export_history(
    source_db, source_instance, source_scope, destination, *, timeout=60
):
    """SQLite Online Backup makes a private consistent read snapshot, then removes it."""
    identifier(source_instance)
    identifier(source_scope)
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeErrorCode("INVALID_HISTORY_TIMEOUT")
    source = Path(source_db).resolve(strict=True)
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700)  # Never overwrite a previous export.
    archive = LocalArtifacts(destination / "objects")
    started = time.monotonic()

    def budget(*_):
        if time.monotonic() - started > timeout:
            raise RuntimeErrorCode("HISTORY_EXPORT_TIMEOUT")

    counts = {
        "tasks": 0,
        "attempts": 0,
        "artifacts": 0,
        "active_tasks": 0,
        "unsettled_tasks": 0,
        "foreign_scope_tasks": 0,
    }
    h = hashlib.sha256()
    size = 0
    with tempfile.TemporaryDirectory(prefix="dios-history-") as temporary:
        snapshot = Path(temporary) / "source.sqlite"
        with _private_file(snapshot):
            pass
        with (
            closing(
                sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)
            ) as incoming,
            closing(sqlite3.connect(snapshot)) as out,
        ):
            incoming.backup(out, pages=256, progress=budget, sleep=0.01)
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeErrorCode("HISTORY_SOURCE_INVALID")
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "a2a_tasks" not in tables:
                raise RuntimeErrorCode("HISTORY_SOURCE_UNSUPPORTED")
            if db.execute(
                "SELECT id FROM a2a_tasks WHERE length(CAST(message AS BLOB))+length(CAST(artifacts AS BLOB)) > ? LIMIT 1",
                (128 * 1024 * 1024,),
            ).fetchone():
                raise RuntimeErrorCode("HISTORY_SOURCE_RECORD_TOO_LARGE")
            with _private_file(destination / "groups.ndjson") as output:
                for raw in db.execute("SELECT * FROM a2a_tasks ORDER BY id"):
                    budget()
                    task = dict(raw)
                    if task["status"] not in TERMINAL:
                        counts["active_tasks"] += 1
                        continue
                    execution = None
                    if "managed_task_executions" in tables:
                        row = db.execute(
                            "SELECT * FROM managed_task_executions WHERE task_id=?",
                            (task["id"],),
                        ).fetchone()
                        execution = dict(row) if row else None
                    calls = []
                    if "managed_service_calls" in tables:
                        calls = [
                            dict(r)
                            for r in db.execute(
                                "SELECT * FROM managed_service_calls WHERE task_id=? ORDER BY id LIMIT 101",
                                (task["id"],),
                            )
                        ]
                        if len(calls) > 100:
                            raise RuntimeErrorCode("HISTORY_GROUP_TOO_LARGE")
                    if (execution and execution["scope"] != source_scope) or any(
                        c["scope"] != source_scope for c in calls
                    ):
                        counts["foreign_scope_tasks"] += 1
                        continue
                    if (execution and not execution["finished"]) or any(
                        c["state"] not in {"completed", "failed"} for c in calls
                    ):
                        counts["unsettled_tasks"] += 1
                        continue
                    artifacts = json.loads(task["artifacts"] or "[]")
                    if not isinstance(artifacts, list):
                        raise RuntimeErrorCode("HISTORY_SOURCE_INVALID")
                    # Bound each transaction before materializing its blobs.
                    if 1 + bool(execution) + len(calls) + len(artifacts) > 100:
                        raise RuntimeErrorCode("HISTORY_GROUP_TOO_LARGE")
                    metadata = {
                        k: task.get(k)
                        for k in (
                            "agent_id",
                            "context_id",
                            "trace_id",
                            "created_at",
                            "updated_at",
                        )
                    }
                    metadata.update(
                        origin_status=task["status"],
                        source_scope=source_scope,
                        scope_evidence="execution_ledger"
                        if execution or calls
                        else "operator_assignment",
                        attempt_evidence=bool(execution or calls),
                        model_session_evidence=False,
                    )
                    records = [
                        _record(
                            archive, "task", task["id"], "a2a_tasks", task, metadata
                        )
                    ]
                    links = []
                    if execution:
                        attempt_id = "task:" + task["id"]
                        records.append(
                            _record(
                                archive,
                                "attempt",
                                attempt_id,
                                "managed_task_executions",
                                execution,
                                {
                                    k: execution[k]
                                    for k in (
                                        "task_id",
                                        "runtime_handle",
                                        "run_id",
                                        "process_instance",
                                        "cancel_requested",
                                        "finished",
                                    )
                                },
                            )
                        )
                        links.append(
                            _link(task["id"], "has_attempt", "attempt", attempt_id)
                        )
                    for call in calls:
                        attempt_id = "call:" + call["id"]
                        records.append(
                            _record(
                                archive,
                                "attempt",
                                attempt_id,
                                "managed_service_calls",
                                call,
                                {
                                    k: call[k]
                                    for k in (
                                        "task_id",
                                        "agent_id",
                                        "state",
                                        "dispatched",
                                        "created_at",
                                        "updated_at",
                                    )
                                },
                            )
                        )
                        links.append(
                            _link(task["id"], "has_attempt", "attempt", attempt_id)
                        )
                    for index, artifact in enumerate(artifacts):
                        artifact_id = hashlib.sha256(
                            encoded([task["id"], index])
                        ).hexdigest()
                        records.append(
                            _record(
                                archive,
                                "artifact",
                                artifact_id,
                                "a2a_tasks.artifacts",
                                artifact,
                                {
                                    "task_id": task["id"],
                                    "index": index,
                                    "origin_artifact_id": artifact.get("artifactId")
                                    if isinstance(artifact, dict)
                                    else None,
                                },
                            )
                        )
                        links.append(
                            _link(task["id"], "has_artifact", "artifact", artifact_id)
                        )
                    group = {"records": records, "links": links}
                    validate_group(source_instance, group)
                    line = encoded(group) + b"\n"
                    size += len(line)
                    if size > MAX_INDEX:
                        raise RuntimeErrorCode("HISTORY_INDEX_TOO_LARGE")
                    output.write(line)
                    h.update(line)
                    counts["tasks"] += 1
                    counts["attempts"] += len(calls) + bool(execution)
                    counts["artifacts"] += len(artifacts)
                output.flush()
                os.fsync(output.fileno())
            counts["unassociated_service_calls"] = (
                db.execute(
                    "SELECT count(*) FROM managed_service_calls c WHERE NOT EXISTS (SELECT 1 FROM a2a_tasks t WHERE t.id=c.task_id)"
                ).fetchone()[0]
                if "managed_service_calls" in tables
                else 0
            )
            counts["orphan_executions"] = (
                db.execute(
                    "SELECT count(*) FROM managed_task_executions e WHERE NOT EXISTS (SELECT 1 FROM a2a_tasks t WHERE t.id=e.task_id)"
                ).fetchone()[0]
                if "managed_task_executions" in tables
                else 0
            )
            counts["total_source_tasks"] = db.execute(
                "SELECT count(*) FROM a2a_tasks"
            ).fetchone()[0]
    manifest = {
        "format": FORMAT,
        "source_instance": source_instance,
        "source_scope": source_scope,
        "groups": {"sha256": h.hexdigest(), "size": size},
        "counts": counts,
        "acceptance": "legacy_unverified",
    }
    data = encoded(manifest)
    with _private_file(destination / "manifest.json") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return {"manifest_digest": hashlib.sha256(data).hexdigest(), **manifest}


def open_bundle(bundle, *, expected_digest=None):
    bundle = Path(bundle).resolve(strict=True)
    path = bundle / "manifest.json"
    if path.is_symlink() or path.stat().st_size > 65536:
        raise RuntimeErrorCode("HISTORY_MANIFEST_INVALID")
    data = path.read_bytes()
    hashed = hashlib.sha256(data).hexdigest()
    if expected_digest is not None and hashed != expected_digest:
        raise RuntimeErrorCode("HISTORY_MANIFEST_CONFLICT")
    manifest = json.loads(data)
    if not isinstance(manifest, dict):
        raise RuntimeErrorCode("HISTORY_MANIFEST_INVALID")
    if (
        manifest.get("format") != FORMAT
        or manifest.get("acceptance") != "legacy_unverified"
        or type(manifest.get("groups", {}).get("size")) is not int
        or not 0 <= manifest["groups"]["size"] <= MAX_INDEX
    ):
        raise RuntimeErrorCode("HISTORY_MANIFEST_INVALID")
    identifier(manifest["source_instance"])
    identifier(manifest["source_scope"])
    counts = manifest.get("counts", {})
    required_counts = {
        "tasks",
        "attempts",
        "artifacts",
        "active_tasks",
        "unsettled_tasks",
        "foreign_scope_tasks",
        "unassociated_service_calls",
        "orphan_executions",
        "total_source_tasks",
    }
    if (
        not isinstance(counts, dict)
        or set(counts) != required_counts
        or any(type(v) is not int or v < 0 for v in counts.values())
        or sum(
            counts[k]
            for k in ("tasks", "active_tasks", "unsettled_tasks", "foreign_scope_tasks")
        )
        != counts["total_source_tasks"]
    ):
        raise RuntimeErrorCode("HISTORY_COUNT_MISMATCH")
    if (
        (bundle / "groups.ndjson").is_symlink()
        or (bundle / "objects").is_symlink()
        or not (bundle / "objects").is_dir()
    ):
        raise RuntimeErrorCode("HISTORY_BUNDLE_INVALID")
    return bundle, manifest, hashed


def groups(stream, source):
    while line := stream.readline(MAX_LINE + 2):
        if len(line) > MAX_LINE + 1 or not line.endswith(b"\n"):
            raise RuntimeErrorCode("HISTORY_GROUP_TOO_LARGE")
        group = json.loads(line)
        validate_group(source, group)
        yield group


def verified_index(bundle, manifest, destination):
    """Copy to a private descriptor to avoid changing the import plan after verification."""
    h, size = hashlib.sha256(), 0
    with (bundle / "groups.ndjson").open("rb") as src:
        while chunk := src.read(65536):
            size += len(chunk)
            if size > MAX_INDEX:
                raise RuntimeErrorCode("HISTORY_INDEX_TOO_LARGE")
            destination.write(chunk)
            h.update(chunk)
    if {"sha256": h.hexdigest(), "size": size} != manifest["groups"]:
        raise RuntimeErrorCode("HISTORY_INDEX_MISMATCH")
    destination.seek(0)
    count = {"tasks": 0, "attempts": 0, "artifacts": 0}
    archive = LocalArtifacts(bundle / "objects")
    for group in groups(destination, manifest["source_instance"]):
        roots = [r for r in group["records"] if r["kind"] == "task"]
        if len(roots) != 1 or len(group["links"]) != len(group["records"]) - 1:
            raise RuntimeErrorCode("HISTORY_SOURCE_INVALID")
        root = roots[0]
        if (
            root["record"].get("origin_status") not in TERMINAL
            or root["record"].get("source_scope") != manifest["source_scope"]
            or any(
                link["kind"] != "task" or link["source_id"] != root["source_id"]
                for link in group["links"]
            )
        ):
            raise RuntimeErrorCode("HISTORY_SOURCE_INVALID")
        for row in group["records"]:
            if row["kind"] == "session":
                raise RuntimeErrorCode("HISTORY_SOURCE_INVALID")
            if row["record"].get("adapter") != FORMAT or row["blobs"] != [
                row["record"].get("source_record")
            ]:
                raise RuntimeErrorCode("HISTORY_SOURCE_INVALID")
            count[row["kind"] + "s"] += 1
            for ref in row["blobs"]:
                if archive.verify(ref["digest"]).size != ref["size"]:
                    raise RuntimeErrorCode("HISTORY_BLOB_SIZE_MISMATCH")
    if any(manifest["counts"].get(k) != v for k, v in count.items()):
        raise RuntimeErrorCode("HISTORY_COUNT_MISMATCH")
    destination.seek(0)
    return archive


def inspect_bundle(bundle):
    bundle, manifest, hashed = open_bundle(bundle)
    with tempfile.TemporaryFile() as index:
        verified_index(bundle, manifest, index)
    return {"manifest_digest": hashed, **manifest}


async def apply_bundle(bundle, expected_digest, database, scope, artifact_root):
    identifier(expected_digest)
    bundle, manifest, hashed = open_bundle(bundle, expected_digest=expected_digest)
    destination = LocalArtifacts(artifact_root)
    from app.runtime.storage_lease import bind_storage
    bind_storage(destination,database)
    count = 0
    with tempfile.TemporaryFile() as index:
        archive = await durable_io(verified_index, bundle, manifest, index)
        for group in groups(index, manifest["source_instance"]):
            await import_group(
                database,
                scope,
                manifest["source_instance"],
                group,
                archive,
                destination,
            )
            count += 1
    return {
        "source_instance": manifest["source_instance"],
        "scope": scope,
        "manifest_digest": hashed,
        "groups_imported": count,
        "acceptance": "legacy_unverified",
        "execution_started": False,
    }


async def main(args):
    if args.command == "export":
        return await asyncio.to_thread(
            export_history,
            args.source_db,
            args.source_instance,
            args.source_scope,
            args.destination,
            timeout=args.timeout,
        )
    if args.command == "inspect":
        return await asyncio.to_thread(inspect_bundle, args.bundle)
    database = Database(args.database_url)
    try:
        return await apply_bundle(
            args.bundle, args.expected_digest, database, args.scope, args.artifacts
        )
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    for key in ("source-db", "source-instance", "source-scope", "destination"):
        export.add_argument("--" + key, required=True)
    export.add_argument("--timeout", type=float, default=60)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("bundle")
    apply = commands.add_parser("import")
    apply.add_argument("bundle")
    for key in ("expected-digest", "database-url", "scope", "artifacts"):
        apply.add_argument("--" + key, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(main(args)), ensure_ascii=False))
    except (
        RuntimeErrorCode,
        OSError,
        sqlite3.Error,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(
            json.dumps(
                {
                    "error": exc.code
                    if isinstance(exc, RuntimeErrorCode)
                    else "HISTORY_ARCHIVE_INVALID"
                }
            )
        )
        raise SystemExit(2) from None
