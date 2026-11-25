"""Read-only catalogs for compatibility state and Worker report spools."""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from app.runtime.database import RuntimeErrorCode
from app.runtime.store import ACTIVE_ATTEMPTS, encoded


def connect(path):
    db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def catalog(db, budget):
    if [tuple(r) for r in db.execute("PRAGMA integrity_check")] != [
        ("ok",)
    ] or db.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeErrorCode("BACKUP_DATABASE_INVALID")
    rows = db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    schemas = [dict(row) for row in rows]
    result = {}
    for row in rows:
        if row["type"] != "table":
            continue
        name = row["name"]
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
            raise RuntimeErrorCode("BACKUP_SCHEMA_INCOMPATIBLE")
        columns = db.execute(f'PRAGMA table_info("{name}")').fetchall()
        keys = [r["name"] for r in sorted(columns, key=lambda r: r["pk"]) if r["pk"]]
        order = ",".join('"' + k + '"' for k in keys) or "rowid"
        hashed, count = hashlib.sha256(), 0
        for record in db.execute(f'SELECT * FROM "{name}" ORDER BY {order}'):
            budget()
            value = {
                k: {"binary_hex": v.hex()} if isinstance(v, bytes) else v
                for k, v in dict(record).items()
            }
            hashed.update(encoded(value) + b"\n")
            count += 1
        result[name] = {"rows": count, "sha256": hashed.hexdigest()}
    return {
        "schema_sha256": hashlib.sha256(encoded(schemas)).hexdigest(),
        "tables": result,
    }


def service_inventory(path, budget):
    from app.models.tables import Base

    db = connect(path)
    try:
        result = catalog(db, budget)
        if not set(Base.metadata.tables) <= result["tables"].keys():
            raise RuntimeErrorCode("BACKUP_SERVICE_SCHEMA_INCOMPATIBLE")
        for name, table in Base.metadata.tables.items():
            actual = {r["name"] for r in db.execute(f'PRAGMA table_info("{name}")')}
            if not set(table.columns.keys()) <= actual:
                raise RuntimeErrorCode("BACKUP_SERVICE_SCHEMA_INCOMPATIBLE")
        relations = {}
        for source, column, target, target_column in (
            ("managed_task_executions", "task_id", "a2a_tasks", "id"),
            ("managed_task_adoptions", "task_id", "a2a_tasks", "id"),
            ("managed_service_owners", "call_id", "managed_service_calls", "id"),
            ("managed_service_results", "call_id", "managed_service_calls", "id"),
        ):
            budget()
            scope = " AND s.scope=t.scope" if target == "managed_service_calls" else ""
            if db.execute(
                f'SELECT 1 FROM "{source}" s LEFT JOIN "{target}" t ON s."{column}"=t."{target_column}"{scope} WHERE t."{target_column}" IS NULL LIMIT 1'
            ).fetchone():
                raise RuntimeErrorCode("BACKUP_SERVICE_REFERENCE_INVALID:" + source)
            relations[source + "." + column] = result["tables"][source]["rows"]
        # Historical Agent deletion is permitted. Preserve and expose that history.
        missing_agents = db.execute(
            "SELECT count(*) FROM a2a_tasks t LEFT JOIN agents a ON t.agent_id=a.id WHERE a.id IS NULL"
        ).fetchone()[0]
        resources = []
        for table, identity, column, kind in (
            ("agents", "id", "workspace_path", "workspace"),
            ("agents", "id", "mcp_config_path", "configuration"),
            ("managed_task_executions", "task_id", "workspace_path", "workspace"),
        ):
            for row in db.execute(
                f'SELECT "{identity}","{column}" FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" != \'\''
            ):
                resources.append(
                    {"table": table, "id": row[0], "kind": kind, "path": row[1]}
                )
        result.update(
            version=1,
            relations=relations,
            historical_missing_agents=missing_agents,
            resources=resources,
            identity_model="compatibility-task-and-native-handle; run_id-is-not-a-Runtime-Run",
        )
        return result
    finally:
        db.close()


def spool_inventory(path, runtime_path, budget):
    db, runtime = connect(path), connect(runtime_path)
    try:
        result = catalog(db, budget)
        if set(result["tables"]) != {"spool_meta", "reports"} or [
            tuple(r) for r in db.execute("SELECT version FROM spool_meta")
        ] != [(1,)]:
            raise RuntimeErrorCode("BACKUP_SPOOL_SCHEMA_INCOMPATIBLE")
        bindings = []
        for row in db.execute("SELECT * FROM reports ORDER BY event_id"):
            budget()
            raw = row["payload"]
            value = json.loads(raw)
            if (
                encoded(value) != raw
                or hashlib.sha256(raw).hexdigest() != row["digest"]
            ):
                raise RuntimeErrorCode("BACKUP_SPOOL_DIGEST_INVALID")
            identity = hashlib.sha256(
                encoded([value["scope"], value["attempt_id"], value["sequence"]])
            ).hexdigest()
            stream = hashlib.sha256(
                encoded([value["scope"], value["attempt_id"]])
            ).hexdigest()
            if (
                identity != row["event_id"]
                or stream != row["stream"]
                or row["sequence"] != value["sequence"]
                or value["phase"] not in ACTIVE_ATTEMPTS
            ):
                raise RuntimeErrorCode("BACKUP_SPOOL_DIGEST_INVALID")
            attempt = runtime.execute(
                "SELECT a.*,r.scope FROM runtime_attempts a JOIN runtime_runs r ON a.run_id=r.id WHERE a.id=?",
                (value["attempt_id"],),
            ).fetchone()
            if attempt is None or any(
                attempt[k] != value[v]
                for k, v in (
                    ("scope", "scope"),
                    ("run_id", "run_id"),
                    ("worker_id", "worker_id"),
                    ("worker_instance", "instance"),
                    ("epoch", "epoch"),
                )
            ):
                raise RuntimeErrorCode("BACKUP_SPOOL_REFERENCE_INVALID")
            bindings.append(
                {
                    "event_id": row["event_id"],
                    "scope": value["scope"],
                    "run_id": value["run_id"],
                    "attempt_id": value["attempt_id"],
                    "sequence": value["sequence"],
                    "already_observed": value["sequence"] <= attempt["last_sequence"],
                }
            )
        result.update(
            version=1,
            bindings=bindings,
            recovery="reauthenticate-and-reconcile-before-delivery; never-execution-authority",
        )
        return result
    except RuntimeErrorCode:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RuntimeErrorCode("BACKUP_SPOOL_INVALID") from None
    finally:
        db.close()
        runtime.close()
