"""Offline mark/sweep of known tombstoned content for a single local installation.

An installation owns one database and artifact root; every current writer holds
both resources for its operation lifetime. Shared/distributed providers require
their own collection protocol. Unknown orphan files are never selected.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .database import RuntimeErrorCode
from .recovery import sqlite_path, _database_info
from .storage_lease import (
    StorageLease,
    database_lock,
    artifacts_lock,
    database_identity,
)
from .store import digest, identifier


def rows(c, sql, limit):
    result = c.execute(sql).fetchmany(limit + 1)
    if len(result) > limit:
        raise RuntimeErrorCode("COLLECTION_INVENTORY_LIMIT")
    return result


def inventory(c, root, limit=100000):
    now = float(c.execute("SELECT (julianday('now')-2440587.5)*86400.0").fetchone()[0])
    facts = []
    for row in rows(
        c,
        "SELECT scope,object_id,version,provider,storage_key,size FROM runtime_object_versions",
        limit,
    ):
        facts.append(
            {
                "ref": [row[0], "file", row[1], row[2]],
                "provider": row[3],
                "key": row[4],
                "size": row[5],
            }
        )
    for row in rows(
        c,
        "SELECT r.scope,a.id,a.provider,a.storage_key,a.size,r.state FROM runtime_artifacts a JOIN runtime_runs r ON a.run_id=r.id",
        limit,
    ):
        facts.append(
            {
                "ref": [row[0], "artifact", row[1], 1],
                "provider": row[2],
                "key": row[3],
                "size": row[4],
                "run_state": row[5],
            }
        )
    if len(facts) > limit:
        raise RuntimeErrorCode("COLLECTION_INVENTORY_LIMIT")
    states = {
        tuple(r[:4]): r[4:]
        for r in rows(
            c,
            "SELECT scope,kind,object_id,object_version,retain_until,expires_at,deleted_at FROM runtime_object_lifetimes",
            limit,
        )
    }
    ancestry = {}
    for scope, kind, oid, version, target in rows(
        c,
        "SELECT scope,source_kind,source_id,source_version,target FROM runtime_object_links WHERE relation='derived_from'",
        limit,
    ):
        value = json.loads(target)
        if "kind" in value:
            ancestry.setdefault((scope, kind, oid, version), []).append(
                (scope, value["kind"], value["id"], value["version"])
            )

    def inactive(start):
        seen = set()
        pending = [(start, ())]
        while pending:
            ref, path = pending.pop()
            if ref in path:
                raise RuntimeErrorCode("OBJECT_DERIVATION_CYCLE")
            if ref in seen:
                continue
            seen.add(ref)
            if len(path) > 64 or len(seen) > 4096:
                raise RuntimeErrorCode("OBJECT_DERIVATION_LIMIT")
            row = states.get(ref)
            if row and (row[2] is not None or row[1] is not None and row[1] <= now):
                return True
            pending.extend((target, path + (ref,)) for target in ancestry.get(ref, []))
        return False

    protected = set()
    eligible = {}
    for fact in facts:
        if fact["provider"] != "local-content-addressed-v1" or not re.fullmatch(
            "[0-9a-f]{64}", fact["key"]
        ):
            raise RuntimeErrorCode("COLLECTION_PROVIDER_UNSUPPORTED")
        ref = tuple(fact["ref"])
        state = states.get(ref)
        if (
            state
            and state[0] > now
            or fact.get("run_state", "succeeded")
            not in {"succeeded", "failed", "cancelled"}
            or not inactive(ref)
        ):
            protected.add(fact["key"])
        else:
            eligible.setdefault(fact["key"], []).append(fact)
    for sql in [
        "SELECT digest FROM runtime_history_archive_objects",
        "SELECT key FROM runtime_remote_chunks UNION SELECT key FROM runtime_remote_uploads",
        "SELECT result_key FROM runtime_tool_invocations WHERE result_key IS NOT NULL",
    ]:
        protected.update(row[0] for row in rows(c, sql, limit))
    # Durable executor/tool receipts can precede database linking. Conservatively
    # retain every digest they name, including receipts no longer used by a task.
    count = 0
    for folder in root.iterdir():
        if folder.name == ".tool-receipts-v1" or folder.name.startswith(".exits-"):
            if folder.is_symlink():
                raise RuntimeErrorCode("COLLECTION_PATH_INVALID")
            for path in folder.rglob("*"):
                if path.is_symlink():
                    raise RuntimeErrorCode("COLLECTION_PATH_INVALID")
                if path.is_dir():
                    continue
                count += 1
                if count > limit or path.stat().st_size > 262144:
                    raise RuntimeErrorCode("COLLECTION_INVENTORY_LIMIT")
                raw = path.read_bytes()
                protected.update(
                    x.decode()
                    for x in re.findall(rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", raw)
                )
    return {"eligible": eligible, "protected": protected, "facts": facts}


def collect(
    url, artifact_root, *, key=None, actor="operator", apply=False, limit=100000
):
    identifier(actor)
    if key is not None:
        identifier(key)
    if apply and not key:
        raise RuntimeErrorCode("COLLECTION_KEY_REQUIRED")
    if type(limit) is not int or not 1 <= limit <= 1000000:
        raise RuntimeErrorCode("INVALID_COLLECTION_LIMIT")
    database = sqlite_path(url)
    root = Path(artifact_root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeErrorCode("COLLECTION_PATH_INVALID")
    root = root.resolve()
    # Refuse immediately while API/Workers/backup processes still hold resources.
    with (
        StorageLease(database_lock(database), exclusive=True),
        StorageLease(artifacts_lock(root), exclusive=True),
    ):
        binding = root / ".dios-databases.json"
        if (
            binding.is_symlink()
            or not binding.is_file()
            or binding.stat().st_size > 65536
            or json.loads(binding.read_text()) != [database_identity(url)]
        ):
            raise RuntimeErrorCode("COLLECTION_ROOT_BINDING_REQUIRED")
        _database_info(database)
        c = sqlite3.connect(database, timeout=0)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA synchronous=FULL")
            c.execute("BEGIN IMMEDIATE")
            schema = c.execute(
                "SELECT name FROM sqlite_master WHERE name='runtime_object_collection_batches'"
            ).fetchone()
            if schema is None:
                raise RuntimeErrorCode("OBJECT_COLLECTION_MIGRATION_REQUIRED")
            active = c.execute(
                "SELECT COUNT(*) FROM runtime_runs WHERE state NOT IN ('succeeded','failed','cancelled')"
            ).fetchone()[0]
            tools = c.execute(
                "SELECT COUNT(*) FROM runtime_tool_invocations WHERE state IN ('dispatched','result_unknown')"
            ).fetchone()[0]
            if active or tools:
                raise RuntimeErrorCode("COLLECTION_ACTIVE_WORK")
            request = digest(
                {
                    "database": str(database),
                    "root": str(root),
                    "actor": actor,
                    "limit": limit,
                }
            )
            old = (
                c.execute(
                    "SELECT * FROM runtime_object_collection_batches WHERE key=?",
                    (key,),
                ).fetchone()
                if key
                else None
            )
            if old and old["digest"] != request:
                raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
            if old and old["report"]:
                return json.loads(old["report"])
            current = inventory(c, root, limit)
            candidates = {
                k: v
                for k, v in current["eligible"].items()
                if k not in current["protected"]
            }
            released = {
                tuple(row)
                for row in c.execute(
                    "SELECT scope,kind,object_id,object_version FROM runtime_object_collections WHERE state='collected'"
                )
            }
            candidates = {
                k: v
                for k, v in candidates.items()
                if (root / k).exists()
                or any(tuple(x["ref"]) not in released for x in v)
            }
            if not apply:
                return {
                    "dry_run": True,
                    "objects": sum(len(v) for v in candidates.values()),
                    "blobs": len(candidates),
                    "bytes": sum(v[0]["size"] for v in candidates.values()),
                    "shared_blobs_retained": len(
                        set(current["eligible"]) & current["protected"]
                    ),
                }
            plan = (
                json.loads(old["plan"])
                if old
                else {
                    "keys": candidates,
                    "shared_blobs_retained": len(
                        set(current["eligible"]) & current["protected"]
                    ),
                }
            )
            for blob, entries in plan["keys"].items():
                target = root / blob
                if target.is_symlink():
                    raise RuntimeErrorCode("COLLECTION_PATH_INVALID")
                if target.exists():
                    hashed = hashlib.sha256()
                    with target.open("rb") as stream:
                        while chunk := stream.read(65536):
                            hashed.update(chunk)
                    if (
                        hashed.hexdigest() != blob
                        or target.stat().st_size != entries[0]["size"]
                    ):
                        raise RuntimeErrorCode("ARTIFACT_DIGEST_MISMATCH")
                elif (
                    not old
                    and not c.execute(
                        "SELECT 1 FROM runtime_object_collections WHERE key=? AND state IN ('pending','collected')",
                        (blob,),
                    ).fetchone()
                ):
                    raise RuntimeErrorCode("COLLECTION_CONTENT_MISSING")
            if not old:
                c.execute(
                    "INSERT INTO runtime_object_collection_batches(key,digest,plan,report) VALUES(?,?,?,NULL)",
                    (key, request, json.dumps(plan)),
                )
                for blob, entries in plan["keys"].items():
                    for entry in entries:
                        c.execute(
                            "INSERT INTO runtime_object_collections VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,kind,object_id,object_version) DO UPDATE SET key=excluded.key,state=excluded.state,operation_key=excluded.operation_key,updated_at=excluded.updated_at",
                            (*entry["ref"], blob, "pending", key, time.time()),
                        )
            c.commit()  # Durable intent precedes unlink; restart rechecks all current references.
            removed = 0
            retained = 0
            size = 0
            for blob, entries in plan["keys"].items():
                state = (
                    "retained_shared" if blob in current["protected"] else "collected"
                )
                if state == "collected":
                    (root / blob).unlink(missing_ok=True)
                    fd = os.open(root, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    removed += 1
                    size += entries[0]["size"]
                else:
                    retained += 1
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "UPDATE runtime_object_collections SET state=?,updated_at=? WHERE operation_key=? AND key=?",
                    (state, time.time(), key, blob),
                )
                c.commit()
            report = {
                "dry_run": False,
                "operation_key": key,
                "collected_blobs": removed,
                "collected_bytes": size,
                "retained_on_resume": retained,
                "released_objects": sum(
                    len(v)
                    for k, v in plan["keys"].items()
                    if k not in current["protected"]
                ),
                "shared_blobs_retained": plan["shared_blobs_retained"],
            }
            c.execute(
                "UPDATE runtime_object_collection_batches SET report=? WHERE key=?",
                (json.dumps(report), key),
            )
            c.commit()
            return report
        finally:
            c.close()
