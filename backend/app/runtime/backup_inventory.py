"""Snapshot-derived inventory, logical reference checks and external recovery needs."""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .database import RuntimeErrorCode
from .revisions import hashes
from .revisions import manifest as revision_manifest
from .store import encoded


def inventory(snapshot, budget=lambda: None):
    db = sqlite3.connect(Path(snapshot).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if re.fullmatch(r"runtime_[a-z0-9_]+", r[0])
        }
        counts = {}
        for table in sorted(tables):
            budget()
            columns = db.execute('PRAGMA table_info("' + table + '")').fetchall()
            keys = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
            order = ",".join('"' + k + '"' for k in keys)
            hashed = hashlib.sha256()
            count = 0
            for row in db.execute('SELECT * FROM "' + table + '" ORDER BY ' + order):
                budget()
                hashed.update(encoded(dict(row)) + b"\n")
                count += 1
            counts[table] = {"rows": count, "sha256": hashed.hexdigest()}

        checked = {}

        def edge(source, column, target, key="id", *, scope=True, extra=""):
            if source not in tables:
                return
            budget()
            label = source + "." + column
            if target not in tables:
                if db.execute(
                    f'SELECT 1 FROM "{source}" WHERE "{column}" IS NOT NULL LIMIT 1'
                ).fetchone():
                    raise RuntimeErrorCode("BACKUP_REFERENCE_INVALID:" + label)
                return
            condition = f's."{column}"=t."{key}"'
            if scope:
                condition += " AND s.scope=t.scope"
            condition += extra
            if db.execute(
                f'SELECT 1 FROM "{source}" s WHERE s."{column}" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "{target}" t WHERE {condition}) LIMIT 1'
            ).fetchone():
                raise RuntimeErrorCode("BACKUP_REFERENCE_INVALID:" + label)
            checked[label] = db.execute(
                f'SELECT COUNT(*) FROM "{source}" WHERE "{column}" IS NOT NULL'
            ).fetchone()[0]

        edge(
            "runtime_runs",
            "attempt_id",
            "runtime_attempts",
            scope=False,
            extra=" AND s.id=t.run_id",
        )
        edge(
            "runtime_artifacts",
            "attempt_id",
            "runtime_attempts",
            scope=False,
            extra=" AND s.run_id=t.run_id",
        )
        for source in (
            "runtime_work_authorizations",
            "runtime_work_decisions",
            "runtime_tool_invocations",
            "runtime_delegation_receipts",
            "runtime_child_runs",
            "runtime_child_handoffs",
        ):
            edge(
                source,
                "parent_run_id" if source == "runtime_child_handoffs" else "run_id",
                "runtime_runs",
            )
        edge(
            "runtime_work_grants",
            "authorization_id",
            "runtime_work_authorizations",
            scope=False,
            extra=" AND s.run_id=t.run_id",
        )
        edge(
            "runtime_work_grants",
            "attempt_id",
            "runtime_attempts",
            scope=False,
            extra=" AND s.run_id=t.run_id AND s.worker_id=t.worker_id AND s.worker_instance=t.worker_instance",
        )
        edge(
            "runtime_tool_invocations",
            "attempt_id",
            "runtime_attempts",
            scope=False,
            extra=" AND s.run_id=t.run_id",
        )
        edge("runtime_tool_evidence", "invocation_id", "runtime_tool_invocations")
        edge("runtime_delegations", "parent_ref", "runtime_delegations")
        edge(
            "runtime_delegation_receipts",
            "authorization_ref",
            "runtime_work_authorizations",
        )
        edge("runtime_delegation_receipts", "leaf_ref", "runtime_delegations")
        edge(
            "runtime_delegation_dependencies",
            "authorization_ref",
            "runtime_work_authorizations",
        )
        edge("runtime_delegation_dependencies", "delegation_ref", "runtime_delegations")
        edge("runtime_child_runs", "parent_run_id", "runtime_runs")
        edge("runtime_child_runs", "root_run_id", "runtime_runs")
        edge(
            "runtime_child_runs",
            "parent_attempt_id",
            "runtime_attempts",
            scope=False,
            extra=" AND s.parent_run_id=t.run_id AND s.parent_session_id=t.session_id",
        )
        edge("runtime_child_handoffs", "delegation_ref", "runtime_delegations")
        edge("runtime_revision_receipts", "revision_id", "runtime_revisions")
        for table in ("runtime_deployments", "runtime_deployment_rollouts"):
            for column in ("revision_id", "previous_revision_id"):
                edge(table, column, "runtime_revisions")
        edge("runtime_deployments", "active_revision_id", "runtime_revisions")
        edge(
            "runtime_deployments",
            "rollout_id",
            "runtime_deployment_rollouts",
            extra=" AND s.id=t.deployment_id",
        )
        edge("runtime_deployment_rollouts", "probe_run_id", "runtime_runs")
        for table in (
            "runtime_deployment_rollouts",
            "runtime_deployment_events",
            "runtime_deployment_receipts",
        ):
            edge(table, "deployment_id", "runtime_deployments")
        for table in ("runtime_deployment_events", "runtime_deployment_receipts"):
            edge(
                table,
                "rollout_id",
                "runtime_deployment_rollouts",
                extra=" AND s.deployment_id=t.deployment_id",
            )

        external = {}

        def need(kind, reference, recovery, scope):
            entry = {
                "kind": kind,
                "reference": reference,
                "scope": scope,
                "recovery": recovery,
            }
            external[hashlib.sha256(encoded(entry)).hexdigest()] = entry

        if "runtime_revisions" in tables:
            for row in db.execute("SELECT * FROM runtime_revisions"):
                budget()
                value = revision_manifest(json.loads(row["manifest"]))
                content, config = hashes(value)
                if content != row["content_digest"] or config != row["config_digest"]:
                    raise RuntimeErrorCode("BACKUP_REVISION_DIGEST_INVALID")
                for kind, dependencies in value["dependencies"].items():
                    for dep in dependencies:
                        if kind in {"image", "model", "harness"}:
                            need(
                                kind,
                                dep["reference"],
                                "reacquire-and-verify"
                                if dep["visibility"] == "pinned"
                                else "reauthenticate-and-reconcile",
                                row["scope"],
                            )
                for ref in value["artifact_refs"]:
                    need("revision-artifact", ref, "resolve-and-verify", row["scope"])
                for ref in value["secret_refs"]:
                    need(
                        "secret",
                        ref,
                        "reauthenticate-secret-provider",
                        row["scope"],
                    )
                profile = value["configuration"].get("execution_profile", {})
                for mount in profile.get("launch", {}).get("mounts", []):
                    need(
                        "workspace",
                        mount["source"],
                        "capture-required-inputs-and-rebind-mount",
                        row["scope"],
                    )

        for run in db.execute("SELECT id,scope,input FROM runtime_runs"):
            budget()
            value = json.loads(run["input"])
            for name, table in (
                ("revision_ref", "runtime_revisions"),
                ("work_authorization_ref", "runtime_work_authorizations"),
                ("deployment_id", "runtime_deployments"),
            ):
                reference = value.get(name)
                if reference is None:
                    continue
                target = (
                    None
                    if table not in tables
                    else db.execute(
                        f'SELECT * FROM "{table}" WHERE id=? AND scope=?',
                        (reference, run["scope"]),
                    ).fetchone()
                )
                if target is None or (
                    name == "work_authorization_ref" and target["run_id"] != run["id"]
                ):
                    raise RuntimeErrorCode(
                        "BACKUP_REFERENCE_INVALID:runtime_runs.input." + name
                    )
                if name == "revision_ref":
                    configuration = json.loads(target["manifest"])["configuration"]
                    if value.get("runtime_profile") != target[
                        "definition_id"
                    ] or value.get("profile_digest") != configuration.get(
                        "profile_digest"
                    ):
                        raise RuntimeErrorCode("BACKUP_REVISION_BINDING_INVALID")
                checked["runtime_runs.input." + name] = (
                    checked.get("runtime_runs.input." + name, 0) + 1
                )
            for ref in value.get("secret_refs", []):
                need(
                    "secret",
                    ref["ref"],
                    "reauthenticate-secret-provider",
                    run["scope"],
                )
            if value.get("runtime_profile") and not value.get("revision_ref"):
                need(
                    "unversioned-profile",
                    value["runtime_profile"],
                    "supply-profile-and-current-policy",
                    run["scope"],
                )
        for row in db.execute(
            "SELECT a.id,a.session_id,a.remote,r.scope FROM runtime_attempts a JOIN runtime_runs r ON a.run_id=r.id"
        ):
            budget()
            remote = json.loads(row["remote"])
            if remote or row["session_id"]:
                need(
                    "execution",
                    row["id"],
                    "collect-worker-spool-and-reconcile-original-process-or-remote-task",
                    row["scope"],
                )

        return {
            "version": 1,
            "tables": counts,
            "references": checked,
            "external": [external[k] for k in sorted(external)],
            "coverage": {
                "database": "runtime-snapshot",
                "configuration": "explicit-file-set",
                "service_database": "separate-backup-and-cross-store-reconciliation-required",
                "worker_spools": "collect-from-each-worker",
                "remote_state": "not-contained",
            },
        }
    finally:
        db.close()


def configuration_files(values):
    """Explicit named files; source paths are not persisted into the portable bundle."""
    result = {}
    total = 0
    for value in values or []:
        name, separator, source = value.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name)
            or name in result
        ):
            raise RuntimeErrorCode("BACKUP_CONFIGURATION_INVALID")
        path = Path(source)
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 16 * 1024 * 1024
        ):
            raise RuntimeErrorCode("BACKUP_CONFIGURATION_INVALID")
        with path.open("rb") as f:
            raw = f.read(16 * 1024 * 1024 + 1)
        total += len(raw)
        if len(raw) > 16 * 1024 * 1024 or total > 32 * 1024 * 1024 or len(result) >= 64:
            raise RuntimeErrorCode("BACKUP_CONFIGURATION_LIMIT")
        result[name] = (path, raw)
    return result
