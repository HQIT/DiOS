"""Coordinate compatibility admission and the existing durable Worker lifecycle."""

import argparse
import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.config import settings
from app.models.tables import (
    A2ATask,
    AgentRuntime,
    ManagedRuntimeControl,
    ManagedServiceCall,
    ManagedServiceResult,
)
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.lifecycle import Lifecycle
from app.services import managed_recovery, managed_runtime

_observations = set()


def _observation_finished(task):
    _observations.discard(task)
    if not task.cancelled():
        task.exception()


class Upgrade:
    def __init__(self, kernel=None):
        self.kernel = kernel

    @asynccontextmanager
    async def kernel_lifecycle(self):
        if self.kernel is not None:
            yield self.kernel
        elif settings.runtime_database_url:
            database = Database(settings.runtime_database_url)
            try:
                yield Lifecycle(database, settings.runtime_scope)
            finally:
                await database.engine.dispose()
        else:
            yield None

    async def status(self):
        from app.db.database import async_session

        async with async_session() as db:
            control = await db.get(ManagedRuntimeControl, settings.runtime_scope)
            mode, revision = (
                (control.mode, control.revision) if control else ("running", 0)
            )
            calls = dict(
                (
                    await db.execute(
                        select(ManagedServiceCall.state, func.count())
                        .where(ManagedServiceCall.scope == settings.runtime_scope)
                        .group_by(ManagedServiceCall.state)
                    )
                ).all()
            )
            pending_calls = (
                (
                    await db.execute(
                        select(ManagedServiceCall)
                        .where(
                            ManagedServiceCall.scope == settings.runtime_scope,
                            ManagedServiceCall.state.in_(["active", "unknown"]),
                        )
                        .order_by(ManagedServiceCall.created_at, ManagedServiceCall.id)
                        .limit(32)
                    )
                )
                .scalars()
                .all()
            )
            call_items = [
                {
                    "id": c.id,
                    "agent_id": c.agent_id,
                    "task_id": c.task_id,
                    "state": c.state,
                    "dispatched": c.dispatched,
                    "error": c.error,
                    "updated_at": (
                        c.updated_at
                        if c.updated_at.tzinfo
                        else c.updated_at.replace(tzinfo=UTC)
                    ).isoformat(),
                }
                for c in pending_calls
            ]
            if pending_calls:
                receipts = {
                    r.call_id: r
                    for r in (
                        await db.execute(
                            select(
                                ManagedServiceResult.call_id,
                                ManagedServiceResult.applied,
                                ManagedServiceResult.record["complete"]
                                .as_boolean()
                                .label("complete"),
                            ).where(
                                ManagedServiceResult.scope == settings.runtime_scope,
                                ManagedServiceResult.call_id.in_(
                                    [c.id for c in pending_calls]
                                ),
                            )
                        )
                    ).all()
                }
                for item in call_items:
                    receipt = receipts.get(item["id"])
                    item["result_retained"] = receipt is not None
                    item["result_applied"] = receipt.applied if receipt else False
                    item["result_complete"] = receipt.complete if receipt else None
            active = (
                await db.execute(
                    select(func.count())
                    .select_from(A2ATask)
                    .where(A2ATask.status.not_in(managed_recovery.TERMINAL))
                )
            ).scalar_one()
            services = (
                (
                    await db.execute(
                        select(AgentRuntime.agent_id).order_by(AgentRuntime.agent_id)
                    )
                )
                .scalars()
                .all()
            )
        recovery = await managed_recovery.status()
        kernel_status = None
        try:
            async with self.kernel_lifecycle() as kernel:
                if kernel is not None:
                    kernel_status = await kernel.status()
        except Exception as exc:  # noqa: BLE001 - unavailable observation prohibits cutover
            kernel_status = {
                "mode": "unknown",
                "ready_for_cutover": False,
                "error": exc.code
                if isinstance(exc, RuntimeErrorCode)
                else type(exc).__name__,
            }
        compatibility_settled = (
            recovery["executions_settled"]
            and active == 0
            and calls.get("active", 0) == 0
            and calls.get("unknown", 0) == 0
        )
        kernel_settled = (
            kernel_status is None or kernel_status.get("ready_for_cutover") is True
        )
        return {
            "scope": settings.runtime_scope,
            "mode": mode,
            "revision": revision,
            "coverage": ["compatibility", "kernel"]
            if kernel_status is not None
            else ["compatibility"],
            "active_protocol_tasks": active,
            "active_service_calls": calls.get("active", 0),
            "unknown_service_calls": calls.get("unknown", 0),
            "service_calls": call_items,
            "observed_at": datetime.now(UTC).isoformat(),
            "service_objects": len(services),
            "services": services,
            "recovery": recovery,
            "kernel": kernel_status,
            "ready_to_stop_services": mode == "draining"
            and compatibility_settled
            and kernel_settled,
            "ready_for_cutover": mode == "draining"
            and compatibility_settled
            and not services
            and kernel_settled,
        }

    async def drain(self):
        from app.db.database import async_session

        async with managed_runtime.serialized("upgrade:" + settings.runtime_scope):
            async with (
                managed_runtime.serialized("admission:" + settings.runtime_scope),
                async_session() as db,
            ):
                row = await db.get(ManagedRuntimeControl, settings.runtime_scope)
                if row is None:
                    row = ManagedRuntimeControl(
                        scope=settings.runtime_scope, mode="draining", revision=1
                    )
                    db.add(row)
                elif row.mode != "draining":
                    row.mode, row.revision = "draining", row.revision + 1
                row.updated_at = datetime.now(UTC)
                await db.commit()
            # A failure here leaves compatibility closed. Retry the same operation.
            async with self.kernel_lifecycle() as kernel:
                if kernel is not None:
                    await kernel.begin_drain()
            return await self.status()

    async def settle_services(self):
        from app.db.database import async_session
        from app.services.agent_runtime import stop_agent

        async with managed_runtime.serialized("upgrade:" + settings.runtime_scope):
            before = await self.status()
            if not before["ready_to_stop_services"]:
                raise RuntimeErrorCode("ACTIVE_OR_UNKNOWN_EXECUTION")
            for agent_id in before["services"]:
                async with async_session() as db:
                    await stop_agent(agent_id, db)
            return await self.status()

    async def resume(self, revision, kernel_revision=None):
        from app.db.database import async_session

        async with managed_runtime.serialized("upgrade:" + settings.runtime_scope):
            async with (
                managed_runtime.serialized("admission:" + settings.runtime_scope),
                async_session() as db,
            ):
                row = await db.get(ManagedRuntimeControl, settings.runtime_scope)
                if not row or row.revision != revision or row.mode != "draining":
                    raise RuntimeErrorCode("VERSION_CONFLICT")
                async with self.kernel_lifecycle() as kernel:
                    if kernel is not None:
                        if kernel_revision is None:
                            raise RuntimeErrorCode("KERNEL_REVISION_REQUIRED")
                        await kernel.resume(kernel_revision)
                row.mode, row.revision = "running", row.revision + 1
                row.updated_at = datetime.now(UTC)
                await db.commit()
            return await self.status()

    async def wait(self, timeout=30):
        if not math.isfinite(timeout) or timeout < 0:
            raise RuntimeErrorCode("INVALID_DRAIN_LIMIT")
        deadline = time.monotonic() + timeout
        while True:
            observation = asyncio.create_task(self.status())
            _observations.add(observation)
            # A caller's observation budget must not cancel an in-flight DB read
            # during cursor/connection cleanup. Let it finish without repeating it.
            observation.add_done_callback(_observation_finished)
            try:
                snapshot = await asyncio.wait_for(
                    asyncio.shield(observation), max(0.01, deadline - time.monotonic())
                )
            except TimeoutError:
                raise RuntimeErrorCode("DRAIN_OBSERVATION_TIMEOUT") from None
            if snapshot["ready_for_cutover"]:
                return snapshot
            if snapshot["mode"] != "draining":
                raise RuntimeErrorCode("DRAIN_NOT_STARTED")
            if time.monotonic() >= deadline:
                raise RuntimeErrorCode("DRAIN_TIMEOUT")
            await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))


async def execute(command, *, revision=None, kernel_revision=None, timeout=30):
    coordinator = Upgrade()
    if command == "status":
        return await coordinator.status()
    if command == "drain":
        return await coordinator.drain()
    if command == "settle-services":
        return await coordinator.settle_services()
    if command == "wait":
        return await coordinator.wait(timeout)
    if command == "resume":
        return await coordinator.resume(revision, kernel_revision)
    raise RuntimeErrorCode("INVALID_UPGRADE_COMMAND")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["status", "drain", "settle-services", "wait", "resume"]
    )
    parser.add_argument("--revision", type=int)
    parser.add_argument("--kernel-revision", type=int)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                asyncio.run(
                    execute(
                        args.command,
                        revision=args.revision,
                        kernel_revision=args.kernel_revision,
                        timeout=args.timeout,
                    )
                ),
                ensure_ascii=False,
            )
        )
    except RuntimeErrorCode as exc:
        print(json.dumps({"error": exc.code}))
        raise SystemExit(2) from None
