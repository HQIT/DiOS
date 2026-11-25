"""Restore a verified installation into an exclusive, execution-held destination."""

import asyncio
import math
import os
import sqlite3
import time
from pathlib import Path

from app.runtime.backup_tree import capture_tree
from app.runtime.database import RuntimeErrorCode
from app.runtime.recovery import _chunks, _file_info, _sync_dir, _write, restore_sqlite
from app.runtime.store import encoded

from .installation_backup import verify_installation
from .installation_catalog import service_inventory, spool_inventory


def prepare(bundle, destination, timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeErrorCode("INVALID_RESTORE_LIMIT")
    manifest = verify_installation(bundle)
    destination.mkdir(mode=0o700)
    started = time.monotonic()

    def budget():
        if time.monotonic() - started > timeout:
            raise RuntimeErrorCode("RESTORE_TIMEOUT")

    def copy(source, target, expected):
        def chunks():
            for chunk in _chunks(source):
                budget()
                yield chunk

        if _write(target, chunks()) != expected:
            raise RuntimeErrorCode("BACKUP_FILE_MISMATCH")

    copy(
        bundle / "service.sqlite",
        destination / "service.pending.sqlite",
        manifest["service"]["database"],
    )
    (destination / "workspaces").mkdir(mode=0o700)
    (destination / "spools").mkdir(mode=0o700)
    mappings = []
    for name, row in manifest["workspaces"].items():
        target = destination / "workspaces" / name
        value = capture_tree(
            bundle / "workspaces" / name,
            target,
            budget,
            max_bytes=manifest["tree_byte_limit"],
        )
        if value != row["inventory"]:
            raise RuntimeErrorCode("BACKUP_TREE_MISMATCH")
        mappings.append(
            {"kind": "workspace", "source": row["source"], "target": str(target)}
        )
    for name, source in manifest["configuration_sources"].items():
        mappings.append(
            {
                "kind": "configuration",
                "source": source,
                "target": str(destination / "runtime/configuration" / name),
            }
        )
    for name, row in manifest["spools"].items():
        copy(
            bundle / "spools" / (name + ".sqlite"),
            destination / "spools" / (name + ".sqlite"),
            row["database"],
        )
    return manifest, mappings


def hold_service(destination, mappings):
    pending = destination / "service.pending.sqlite"
    with sqlite3.connect(pending) as db:
        # Any restore_hold row closes every scope, including scopes first seen after restore.
        db.execute(
            "INSERT INTO managed_runtime_control(scope,mode,revision,updated_at) VALUES(?,?,1,datetime('now')) ON CONFLICT(scope) DO UPDATE SET mode=excluded.mode,revision=revision+1,updated_at=excluded.updated_at",
            ("__dios_installation_restore__", "restore_hold"),
        )
        revoked = db.execute(
            "UPDATE e2ag_tool_grants SET status='revoked' WHERE status='active'"
        ).rowcount
        relocated = 0
        for table, identity, column in (
            ("agents", "id", "workspace_path"),
            ("agents", "id", "mcp_config_path"),
            ("managed_task_executions", "task_id", "workspace_path"),
        ):
            for key, path in db.execute(
                f'SELECT "{identity}","{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            ).fetchall():
                candidates = [
                    m
                    for m in mappings
                    if path == m["source"]
                    or (
                        m["kind"] == "workspace"
                        and Path(m["source"]) in Path(path).parents
                    )
                ]
                if candidates:
                    match = max(candidates, key=lambda m: len(m["source"]))
                    new = str(
                        Path(match["target"]) / Path(path).relative_to(match["source"])
                    )
                    db.execute(
                        f'UPDATE "{table}" SET "{column}"=? WHERE "{identity}"=?',
                        (new, key),
                    )
                    relocated += 1
        db.commit()
    return {"revoked_tool_grants": revoked, "relocated_paths": relocated}


async def restore_installation(bundle, destination, *, timeout=120):
    started = time.monotonic()
    bundle, destination = Path(bundle).resolve(), Path(destination).resolve()
    if bundle == destination or bundle in destination.parents:
        raise RuntimeErrorCode("RESTORE_DESTINATION_INSIDE_BACKUP")
    manifest, mappings = await asyncio.to_thread(prepare, bundle, destination, timeout)
    runtime = await restore_sqlite(bundle / "runtime", destination / "runtime")

    def publish():
        if time.monotonic() - started > timeout:
            raise RuntimeErrorCode("RESTORE_TIMEOUT")
        service = hold_service(destination, mappings)
        inventory = service_inventory(
            destination / "service.pending.sqlite", lambda: None
        )
        spools = {
            name: spool_inventory(
                destination / "spools" / (name + ".sqlite"),
                destination / "runtime/state.sqlite",
                lambda: None,
            )
            for name in manifest["spools"]
        }
        # The source Runtime manifest remains immutable, including versioned workspace paths.
        receipt = {
            "format": 1,
            "mode": "restore_hold",
            "source_installation": _file_info(bundle / "installation.json"),
            "runtime": runtime,
            "service": {
                **service,
                "database": _file_info(destination / "service.pending.sqlite"),
                "inventory": inventory,
            },
            "path_mappings": mappings,
            "spools": spools,
            "spool_delivery": "quarantined-until-reauthentication-and-reconciliation",
            "external_recovery": manifest["external"],
            "resource_bindings": manifest["resource_bindings"],
            "requires": [
                "source-controller-isolation",
                "remote-reference-reauthentication",
                "task-and-effect-reconciliation",
                "immutable-revision-path-review",
            ],
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        with (destination / "service.pending.sqlite").open("rb") as f:
            os.fsync(f.fileno())
        os.link(destination / "service.pending.sqlite", destination / "service.sqlite")
        (destination / "service.pending.sqlite").unlink()
        _sync_dir(destination / "spools")
        _sync_dir(destination / "workspaces")
        _write(destination / "installation-restore.json", [encoded(receipt)])
        _sync_dir(destination)
        _sync_dir(destination.parent)
        return receipt

    return await asyncio.to_thread(publish)
