"""Reconcile persisted compatibility executions; never launch or replay an Agent."""

import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from app.config import settings
from app.models.tables import A2ATask, ManagedTaskExecution
from app.runtime.database import RuntimeErrorCode
from app.services import managed_runtime

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "canceled"}


async def register(db, task_id, runtime_handle, workspace_path, run_id):
    values = {
        "scope": settings.runtime_scope,
        "runtime_handle": runtime_handle,
        "workspace_path": str(Path(workspace_path).resolve()),
        "run_id": run_id,
    }
    execution = await db.get(ManagedTaskExecution, task_id)
    if execution is None:
        execution = ManagedTaskExecution(task_id=task_id, **values)
        db.add(execution)
    elif any(getattr(execution, key) != value for key, value in values.items()):
        raise RuntimeErrorCode("MANAGED_EXECUTION_CONFLICT")
    return execution


async def _record_error(task_id, error):
    from app.db.database import async_session

    async with async_session() as db:
        execution = await db.get(ManagedTaskExecution, task_id)
        if (
            execution
            and execution.scope == settings.runtime_scope
            and not execution.finished
        ):
            execution.error = error
            await db.commit()


async def reconcile(task_id):
    from app.db.database import async_session
    from app.services.a2a_service import (
        _cancel_task_locked,
        _finish_task_runtime,
        _remove_task_secret_config,
    )

    async with async_session() as db:
        execution = await db.get(ManagedTaskExecution, task_id)
        if execution is None or execution.scope != settings.runtime_scope:
            raise RuntimeErrorCode("MANAGED_EXECUTION_NOT_FOUND")
        runtime_handle = execution.runtime_handle
    try:
        async with managed_runtime.exclusive(runtime_handle), async_session() as db:
            execution = await db.get(ManagedTaskExecution, task_id)
            if execution.finished:
                return "finished"
            task = await db.get(A2ATask, task_id)
            if task is None:
                raise RuntimeErrorCode("TASK_RECORD_MISSING")
            adapter = managed_runtime.managed_adapter()
            observed = await adapter.observe(runtime_handle)
            execution.observation = asdict(observed)
            execution.observed_at = datetime.now(UTC)
            execution.error = ""
            if (
                observed.process_instance
                and execution.process_instance
                and observed.process_instance != execution.process_instance
            ):
                await db.commit()
                raise RuntimeErrorCode("PROCESS_CHANGED")
            if observed.process_instance:
                execution.process_instance = observed.process_instance
            await db.commit()
            if execution.cancel_requested and task.status not in TERMINAL:
                await _cancel_task_locked(db, task_id)
                return "finished"
            if task.status in TERMINAL:
                if observed.alive:
                    raise RuntimeErrorCode("TERMINAL_RUNTIME_STILL_RUNNING")
                if observed.state not in {"absent", "exited", "dead"}:
                    raise RuntimeErrorCode("RUNTIME_EXIT_UNCONFIRMED")
            elif observed.state == "absent":
                task.error = execution.error = "RUNTIME_OBSERVATION_ABSENT"
                await db.commit()
                return "absent"
            elif observed.alive or observed.state not in {"exited", "dead"}:
                return "pending"
            else:
                snapshot = await managed_runtime.retain_logs(
                    runtime_handle,
                    Path(execution.workspace_path),
                    observed.process_instance,
                )
                logs = (snapshot.stdout + b"\n" + snapshot.stderr).decode(
                    "utf-8", errors="replace"
                )
                await _finish_task_runtime(
                    task_id,
                    execution.run_id,
                    execution.workspace_path,
                    observed.exit_code,
                    logs,
                )
            await adapter.remove(runtime_handle)
            _remove_task_secret_config(execution.workspace_path, execution.run_id)
            execution.finished = True
            execution.error = ""
            await db.commit()
            return "finished"
    except RuntimeErrorCode as exc:
        if exc.code == "RUNTIME_OPERATION_IN_PROGRESS":
            return "busy"
        await _record_error(task_id, exc.code)
        return "error"
    except Exception as exc:  # noqa: BLE001 - retain uncertainty across provider/storage failures
        await _record_error(
            task_id, "RUNTIME_OBSERVATION_UNAVAILABLE:" + type(exc).__name__
        )
        return "error"


async def status():
    from app.db.database import async_session

    async with async_session() as db:
        pending = (
            await db.execute(
                select(func.count())
                .select_from(ManagedTaskExecution)
                .where(
                    ManagedTaskExecution.scope == settings.runtime_scope,
                    ManagedTaskExecution.finished.is_(False),
                )
            )
        ).scalar_one()
        unknown = (
            await db.execute(
                select(func.count())
                .select_from(ManagedTaskExecution)
                .where(
                    ManagedTaskExecution.scope == settings.runtime_scope,
                    ManagedTaskExecution.finished.is_(False),
                    ManagedTaskExecution.error != "",
                )
            )
        ).scalar_one()
        # Pre-ledger tasks require explicit inspection/mapping, never blind relaunch.
        untracked = (
            await db.execute(
                select(func.count())
                .select_from(A2ATask)
                .outerjoin(
                    ManagedTaskExecution, ManagedTaskExecution.task_id == A2ATask.id
                )
                .where(
                    A2ATask.status.not_in(TERMINAL),
                    ManagedTaskExecution.task_id.is_(None),
                )
            )
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    select(ManagedTaskExecution)
                    .where(
                        ManagedTaskExecution.scope == settings.runtime_scope,
                        ManagedTaskExecution.finished.is_(False),
                    )
                    .order_by(ManagedTaskExecution.task_id)
                    .limit(32)
                )
            )
            .scalars()
            .all()
        )
        items = [
            {
                "task_id": row.task_id,
                "cancel_requested": row.cancel_requested,
                "state": (row.observation or {}).get("state", "unobserved"),
                "observed_at": row.observed_at.replace(
                    tzinfo=row.observed_at.tzinfo or UTC
                )
                .astimezone(UTC)
                .isoformat()
                if row.observed_at
                else None,
                "process_instance": row.process_instance,
                "error": row.error,
            }
            for row in rows
        ]
    return {
        "scope": settings.runtime_scope,
        "pending_executions": pending,
        "unresolved_executions": unknown,
        "untracked_active_tasks": untracked,
        "executions_settled": pending == 0 and untracked == 0,
        "items": items,
        "items_limit": 32,
    }


class ManagedRecovery:
    def __init__(self, *, interval=5, batch_size=32):
        if interval <= 0 or not 1 <= batch_size <= 100:
            raise ValueError("Invalid recovery limits")
        self.interval, self.batch_size = interval, batch_size
        self.cursor = ""
        self.service_cursor = ""
        self.task = None
        self._wake = asyncio.Event()
        self._stopping = False

    async def tick(self):
        from app.db.database import async_session

        async with async_session() as db:
            query = select(ManagedTaskExecution.task_id).where(
                ManagedTaskExecution.scope == settings.runtime_scope,
                ManagedTaskExecution.finished.is_(False),
            )
            if self.cursor:
                query = query.where(ManagedTaskExecution.task_id > self.cursor)
            ids = (
                (
                    await db.execute(
                        query.order_by(ManagedTaskExecution.task_id).limit(
                            self.batch_size
                        )
                    )
                )
                .scalars()
                .all()
            )
        self.cursor = ids[-1] if len(ids) == self.batch_size else ""
        results = {}
        for task_id in ids:
            results[task_id] = await reconcile(task_id)
        from app.services.managed_service_recovery import batch
        services, self.service_cursor = await batch(self.service_cursor, self.batch_size)
        results.update(services)
        return results

    def wake(self):
        self._wake.set()

    async def _loop(self):
        while not self._stopping:
            self._wake.clear()
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - retain uncertainty across provider/storage failures
                logger.warning("Managed recovery unavailable: %s", type(exc).__name__)
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), self.interval)
            except TimeoutError:
                pass

    def start(self):
        if self.task is None or self.task.done():
            self._stopping = False
            self._wake = asyncio.Event()
            self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stopping = True
        self.wake()
        if self.task:
            await (
                self.task
            )  # Finish the current native operation; never stop Agent processes.
            self.task = None


controller = ManagedRecovery()


async def _main(command, limit):
    if command == "reconcile":
        results = await ManagedRecovery(batch_size=limit).tick()
        return {"results": results, "status": await status()}
    return await status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "reconcile"])
    parser.add_argument("--limit", type=int, default=32)
    arguments = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(_main(arguments.command, arguments.limit)), ensure_ascii=False
        )
    )
