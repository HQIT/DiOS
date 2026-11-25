"""Compose SQLite snapshots, immutable Runtime objects and explicitly owned files."""

import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

from app.runtime.backup_tree import capture_tree, inspect_tree
from app.runtime.database import RuntimeErrorCode
from app.runtime.recovery import (
    _file_info,
    _sync_dir,
    _write,
    backup_sqlite,
    sqlite_path,
    verify_backup,
)
from app.runtime.store import encoded

from .installation_catalog import service_inventory, spool_inventory

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


def local_spools(artifacts):
    found = {}
    for path in Path(artifacts).glob(".reports-*.sqlite"):
        if path.is_symlink() or not re.fullmatch(
            r"\.reports-[a-f0-9]{64}\.sqlite", path.name
        ):
            raise RuntimeErrorCode("BACKUP_SPOOL_FILE_INVALID")
        found["local-" + path.name[9:-7]] = path.resolve()
    return found


def named(values):
    if not isinstance(values, list) or len(values) > 64:
        raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
    result = {}
    for row in values:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "path"}
            or not isinstance(row["id"], str)
            or not NAME.fullmatch(row["id"])
            or row["id"] in result
        ):
            raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
        if not isinstance(row["path"], str) or not row["path"]:
            raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
        path = Path(row["path"])
        if not path.is_absolute() or path.is_symlink():
            raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
        result[row["id"]] = path.resolve()
    return result


def plan_value(value):
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "format",
            "runtime_database_url",
            "service_database_url",
            "artifacts",
            "configuration",
            "workspaces",
            "spools",
            "external",
        }
        or type(value["format"]) is not int
        or value["format"] != 1
    ):
        raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
    for key in ("runtime_database_url", "service_database_url", "artifacts"):
        if not isinstance(value[key], str) or not value[key]:
            raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
    for key in ("configuration", "workspaces", "spools"):
        named(value[key])
    if not isinstance(value["external"], list) or len(value["external"]) > 256:
        raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
    for row in value["external"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"kind", "reference", "recovery"}
            or any(
                not isinstance(v, str) or not v or len(v) > 4096 for v in row.values()
            )
        ):
            raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
    return value


def snapshot(source, destination, budget):
    if source.is_symlink() or not source.is_file():
        raise RuntimeErrorCode("BACKUP_FILE_INVALID")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    incoming = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)
    outgoing = sqlite3.connect(destination)
    try:
        incoming.backup(outgoing, pages=256, progress=lambda *_: budget(), sleep=0.01)
        outgoing.execute("PRAGMA journal_mode=DELETE")
    finally:
        outgoing.close()
        incoming.close()
    with destination.open("rb") as f:
        os.fsync(f.fileno())


def resources(service, runtime, configuration, workspaces, external, trees):
    needs = list(service["resources"])
    needs += [
        {
            "kind": "workspace",
            "path": r["reference"],
            "table": "runtime_revisions",
            "id": r["scope"],
        }
        for r in runtime["inventory"]["external"]
        if r["kind"] == "workspace"
    ]
    declared = {(r["kind"], r["reference"]): r["recovery"] for r in external}
    result = []
    for need in needs:
        path = Path(need["path"])
        included = None
        if path.is_absolute():
            for name, root in workspaces.items():
                if path == root or root in path.parents:
                    relative = path.relative_to(root).as_posix()
                    inventory = trees[name]["inventory"]
                    if (
                        relative == "."
                        or relative in inventory["files"]
                        or relative in inventory["directories"]
                    ):
                        included = "workspace:" + name
                        break
            included = next(
                (
                    "configuration:" + name
                    for name, source in configuration.items()
                    if path == source
                ),
                included,
            )
        recovery = declared.get((need["kind"], need["path"]))
        if included is None and recovery is None:
            raise RuntimeErrorCode(
                "BACKUP_RESOURCE_UNDECLARED:" + need["table"] + ":" + need["id"]
            )
        result.append(
            dict(
                need,
                included=included,
                external_recovery=recovery if included is None else None,
            )
        )
    return result


def backup_installation(value, destination, *, timeout=120, max_tree_bytes=1024**3):
    value = plan_value(value)
    started = time.monotonic()
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or type(max_tree_bytes) is not int
        or max_tree_bytes < 1
    ):
        raise RuntimeErrorCode("INVALID_BACKUP_LIMIT")

    def budget():
        if time.monotonic() - started > timeout:
            raise RuntimeErrorCode("BACKUP_TIMEOUT")

    sqlite_path(value["runtime_database_url"])
    service = sqlite_path(value["service_database_url"])
    configuration, workspaces, spools = (
        named(value[k]) for k in ("configuration", "workspaces", "spools")
    )
    discovered = local_spools(value["artifacts"])
    for name, path in discovered.items():
        if path not in spools.values():
            if name in spools:
                raise RuntimeErrorCode("BACKUP_INSTALLATION_PLAN_INVALID")
            spools[name] = path
    root = Path(destination).resolve()
    for directory in [Path(value["artifacts"]).resolve(), *workspaces.values()]:
        if root == directory or directory in root.parents:
            raise RuntimeErrorCode("BACKUP_DESTINATION_INSIDE_SOURCE")
    root.mkdir(mode=0o700)
    (root / "spools").mkdir(mode=0o700)
    (root / "workspaces").mkdir(mode=0o700)
    # Earlier spool reports must refer to durable Attempts present in the later Runtime snapshot.
    for name, path in spools.items():
        budget()
        snapshot(path, root / "spools" / (name + ".sqlite"), budget)
    snapshot(service, root / "service.sqlite", budget)
    runtime = backup_sqlite(
        value["runtime_database_url"],
        value["artifacts"],
        root / "runtime",
        timeout=max(0.001, timeout - (time.monotonic() - started)),
        config_files=[name + "=" + str(path) for name, path in configuration.items()],
    )
    service_catalog = service_inventory(root / "service.sqlite", budget)
    spool_catalog = {
        name: {
            "database": _file_info(root / "spools" / (name + ".sqlite")),
            "inventory": spool_inventory(
                root / "spools" / (name + ".sqlite"),
                root / "runtime/state.sqlite",
                budget,
            ),
        }
        for name in spools
    }
    tree_catalog = {}
    total = 0
    for name, path in workspaces.items():
        budget()
        tree = capture_tree(
            path, root / "workspaces" / name, budget, max_bytes=max_tree_bytes - total
        )
        total += tree["bytes"]
        tree_catalog[name] = {"source": str(path), "inventory": tree}
    # A directory may have changed while a later directory was captured.
    for name, path in workspaces.items():
        if (
            inspect_tree(path, budget, max_bytes=max_tree_bytes)
            != tree_catalog[name]["inventory"]
        ):
            raise RuntimeErrorCode("BACKUP_TREE_CHANGED")
    for name, path in configuration.items():
        if _file_info(path) != runtime["configuration"][name]:
            raise RuntimeErrorCode("BACKUP_CONFIGURATION_CHANGED")
    if local_spools(value["artifacts"]) != discovered:
        raise RuntimeErrorCode("BACKUP_SPOOL_SET_CHANGED")
    bindings = resources(
        service_catalog,
        runtime,
        configuration,
        workspaces,
        value["external"],
        tree_catalog,
    )
    manifest = {
        "format": 1,
        "provider": "sqlite-installation-v1",
        "created_at": time.time(),
        "runtime_manifest": _file_info(root / "runtime/manifest.json"),
        "service": {
            "database": _file_info(root / "service.sqlite"),
            "inventory": service_catalog,
        },
        "configuration_sources": {
            name: str(path) for name, path in configuration.items()
        },
        "spools": spool_catalog,
        "workspaces": tree_catalog,
        "resource_bindings": bindings,
        "external": value["external"],
        "tree_byte_limit": max_tree_bytes,
        "consistency": "component-snapshots-with-verified-references; independent-databases-have-no-shared-transaction",
        "recovery": "fence-original-controllers-and-reauthenticate-before-any-execution-or-spool-delivery",
        "duration_seconds": time.monotonic() - started,
    }
    raw = encoded(manifest)
    if len(raw) > 16 * 1024 * 1024:
        raise RuntimeErrorCode("BACKUP_MANIFEST_TOO_LARGE")
    _sync_dir(root / "spools")
    _sync_dir(root / "workspaces")
    _write(root / "installation.json", [raw])
    _sync_dir(root)
    _sync_dir(root.parent)
    return manifest


def verify_installation(bundle):
    root = Path(bundle).resolve()
    path = root / "installation.json"
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 16 * 1024 * 1024
    ):
        raise RuntimeErrorCode("BACKUP_INSTALLATION_MANIFEST_INVALID")
    for name in ("runtime", "spools", "workspaces"):
        if (root / name).is_symlink():
            raise RuntimeErrorCode("BACKUP_FILE_INVALID")
    try:
        value = json.loads(path.read_text())
        if (
            type(value["format"]) is not int
            or value["format"] != 1
            or value["provider"] != "sqlite-installation-v1"
        ):
            raise RuntimeErrorCode("BACKUP_INSTALLATION_MANIFEST_INVALID")
        if (
            _file_info(root / "runtime/manifest.json") != value["runtime_manifest"]
            or _file_info(root / "service.sqlite") != value["service"]["database"]
        ):
            raise RuntimeErrorCode("BACKUP_DATABASE_MISMATCH")
        runtime = verify_backup(root / "runtime")
        budget = lambda: None
        actual_service = service_inventory(root / "service.sqlite", budget)
        if actual_service != value["service"]["inventory"]:
            raise RuntimeErrorCode("BACKUP_INVENTORY_MISMATCH")
        for name, row in value["spools"].items():
            if not NAME.fullmatch(name):
                raise RuntimeErrorCode("BACKUP_INSTALLATION_MANIFEST_INVALID")
            snapshot_path = root / "spools" / (name + ".sqlite")
            if (
                _file_info(snapshot_path) != row["database"]
                or spool_inventory(snapshot_path, root / "runtime/state.sqlite", budget)
                != row["inventory"]
            ):
                raise RuntimeErrorCode("BACKUP_SPOOL_MISMATCH")
        for name, row in value["workspaces"].items():
            if not NAME.fullmatch(name):
                raise RuntimeErrorCode("BACKUP_INSTALLATION_MANIFEST_INVALID")
            if (
                inspect_tree(
                    root / "workspaces" / name,
                    budget,
                    max_bytes=value["tree_byte_limit"],
                )
                != row["inventory"]
            ):
                raise RuntimeErrorCode("BACKUP_TREE_MISMATCH")
        sources = {
            name: Path(row["source"]) for name, row in value["workspaces"].items()
        }
        configuration = {
            name: Path(p) for name, p in value["configuration_sources"].items()
        }
        if set(configuration) != set(runtime["configuration"]):
            raise RuntimeErrorCode("BACKUP_CONFIGURATION_MISMATCH")
        if (
            resources(
                actual_service,
                runtime,
                configuration,
                sources,
                value["external"],
                value["workspaces"],
            )
            != value["resource_bindings"]
        ):
            raise RuntimeErrorCode("BACKUP_RESOURCE_BINDING_MISMATCH")
        return value
    except RuntimeErrorCode:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RuntimeErrorCode("BACKUP_INSTALLATION_MANIFEST_INVALID") from None
    except FileNotFoundError:
        raise RuntimeErrorCode("BACKUP_FILE_MISSING") from None
