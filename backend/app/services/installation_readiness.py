"""Read-only recovery observation; never grants execution authority."""

import json
import time
from contextlib import ExitStack
from pathlib import Path

from app.runtime.database import RuntimeErrorCode

from .installation_catalog import connect


def inspect_restore(destination, *, sample_limit=20):
    if type(sample_limit) is not int or not 1 <= sample_limit <= 100:
        raise RuntimeErrorCode("INVALID_RECOVERY_SAMPLE_LIMIT")
    root = Path(destination).resolve()
    receipt = json.loads((root / "installation-restore.json").read_text())
    if receipt.get("format") != 1 or receipt.get("mode") != "restore_hold":
        raise RuntimeErrorCode("INVALID_INSTALLATION_RESTORE_RECEIPT")
    started = time.time()
    observations = {}
    with ExitStack() as stack:

        def database(path):
            db = connect(path)
            stack.callback(db.close)
            # Stable observations within each component. This is not a global cut.
            db.execute("BEGIN")
            return db

        runtime = database(root / "runtime/state.sqlite")
        service = database(root / "service.sqlite")
        held = runtime.execute(
            "SELECT mode FROM runtime_lifecycle WHERE key='global'"
        ).fetchone()
        runtime_held = held is not None and held[0] == "restore_hold"
        service_held = (
            service.execute(
                "SELECT 1 FROM managed_runtime_control WHERE mode='restore_hold' LIMIT 1"
            ).fetchone()
            is not None
        )

        def observe(name, db, query):
            count = db.execute("SELECT count(*) FROM (" + query + ")").fetchone()[0]
            items = [
                dict(r)
                for r in db.execute(query + " ORDER BY 1 LIMIT ?", (sample_limit,))
            ]
            observations[name] = {
                "count": count,
                "sample": items,
                "truncated": count > len(items),
            }

        observe(
            "unsettled_runs",
            runtime,
            "SELECT id,scope,state FROM runtime_runs WHERE state NOT IN ('queued','succeeded','failed','cancelled')",
        )
        observe(
            "unsettled_attempts",
            runtime,
            "SELECT id,run_id,state FROM runtime_attempts WHERE state NOT IN ('completed','failed','cancelled','abandoned')",
        )
        observe(
            "unknown_tool_effects",
            runtime,
            "SELECT id,scope,run_id,state FROM runtime_tool_invocations WHERE state IN ('dispatched','result_unknown')",
        )
        observe(
            "active_worker_credentials",
            runtime,
            "SELECT id,run_id,state FROM runtime_worker_credentials WHERE state='active'",
        )
        observe(
            "unfinished_tasks",
            service,
            "SELECT id,status FROM a2a_tasks WHERE status NOT IN ('completed','failed','canceled')",
        )
        observe(
            "unfinished_executions",
            service,
            "SELECT task_id,scope FROM managed_task_executions WHERE finished=0",
        )
        observe(
            "unsettled_service_calls",
            service,
            "SELECT id,scope,state FROM managed_service_calls WHERE state IN ('active','unknown')",
        )
        observe(
            "unapplied_service_results",
            service,
            "SELECT call_id,scope FROM managed_service_results WHERE applied=0",
        )
        observe(
            "retained_agent_runtimes",
            service,
            "SELECT agent_id,status FROM agent_runtimes",
        )
        observe(
            "active_tool_grants",
            service,
            "SELECT id,task_id FROM e2ag_tool_grants WHERE status='active'",
        )
        # Enumerate actual files as well as declared files; missing captured spools fail.
        declared = set(receipt["spools"])
        actual = {p.stem for p in (root / "spools").glob("*.sqlite")}
        if not declared <= actual:
            raise RuntimeErrorCode("RESTORE_SPOOL_MISSING")
        for name in sorted(actual):
            db = database(root / "spools" / (name + ".sqlite"))
            observe(
                "spool:" + name, db, "SELECT event_id,sequence,blocked FROM reports"
            )
    pending = [name for name, value in observations.items() if value["count"]]
    return {
        "format": 1,
        "observation_started_at": started,
        "observed_at": time.time(),
        "snapshot_scope": "per-component",
        "holds": {"runtime": runtime_held, "service": service_held},
        "observations": observations,
        "pending_local_checks": pending,
        "local_execution_settled": runtime_held and service_held and not pending,
        "external_dependencies": len(receipt["external_recovery"]),
        "requires_external_verification": [
            "source-controller-and-executor-isolation",
            "checkpoint-and-post-checkpoint-effect-reconciliation",
            "remote-reference-reauthentication",
            "immutable-revision-path-and-external-resource-review",
        ],
        "ready_to_activate": False,
    }
