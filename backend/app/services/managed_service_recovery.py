"""Native caller ownership and recovery of interrupted shared-service requests."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import anyio
from sqlalchemy import select

from app.config import settings
from app.models.tables import (
    A2ATask,
    ManagedServiceCall,
    ManagedServiceOwner,
    ManagedServiceResult,
)
from app.runtime.database import RuntimeErrorCode
from app.services import managed_admission, managed_runtime


class CallOwner:
    def __init__(self):
        self.id = uuid4().hex
        self.lock = managed_runtime.exclusive("service-call:" + self.id)
        self.closed = False
        self.started = False
        self.closing = asyncio.Lock()

    async def begin(self, db, agent_id, *, task_id=None):
        await self.lock.__aenter__()
        try:
            await managed_admission.begin_call(
                db, agent_id, task_id=task_id, owner_id=self.id
            )
            self.started = True
        except BaseException:
            await self.lock.__aexit__(None, None, None)
            raise
        return self.id

    async def close(self, error="SERVICE_CALL_INTERRUPTED"):
        # Starlette uses level cancellation; complete persistence before releasing
        # the lock. A process kill releases the native lock for the reconciler.
        with anyio.CancelScope(shield=True):
            async with self.closing:
                if self.closed or not self.started:
                    return
                try:
                    await interrupted(self.id, error)
                finally:
                    self.closed = True
                    await self.lock.__aexit__(None, None, None)


def reply_content(data):
    """A transport-level success must still contain a complete textual result."""
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
        valid = (
            isinstance(content, str)
            and bool(content.strip())
            and choice.get("finish_reason") in {None, "stop"}
        )
    except (KeyError, IndexError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise RuntimeErrorCode("SERVICE_RESULT_INCOMPLETE")
    return content


async def interrupted(call_id, error):
    from app.db.database import async_session
    from app.services.e2ag_tool_gateway import revoke_task_grants

    async with async_session() as db:
        call = await managed_admission.finish_in_session(db, call_id, error=error)
        if call.task_id and call.state in {"unknown", "failed"}:
            task = await db.get(A2ATask, call.task_id)
            if task and task.status in {"working", "submitted"}:
                task.error = call.error
                task.updated_at = call.updated_at
                if call.state == "failed":
                    task.status = "failed"
                await revoke_task_grants(db, task.id)
        await db.commit()


async def reconcile(call_id):
    from app.services.managed_admission import assert_not_restoring
    from app.db.database import async_session
    from app.services import managed_service_results
    from app.services.e2ag_tool_gateway import revoke_task_grants

    try:
        async with managed_runtime.exclusive("service-call:" + call_id):  # noqa: SIM117 - keep lock and DB lifetimes explicit
            async with async_session() as db:
                await assert_not_restoring(db)
                owner = await db.get(ManagedServiceOwner, call_id)
                call = await db.get(ManagedServiceCall, call_id)
                if (
                    not owner
                    or owner.scope != settings.runtime_scope
                    or not call
                    or call.scope != settings.runtime_scope
                ):
                    raise RuntimeErrorCode("SERVICE_CALL_OWNERSHIP_UNVERIFIED")
                if call.state not in {"active", "unknown"}:
                    return call.state
                if await db.get(ManagedServiceResult, call_id):
                    # Release this read transaction before the atomic projection.
                    await db.rollback()
                    return await managed_service_results.apply(call_id)
                if call.state == "unknown":
                    return "unknown"
                call.state = "unknown" if call.dispatched else "failed"
                call.error = (
                    "SERVICE_CALL_OWNER_LOST_AFTER_DISPATCH"
                    if call.dispatched
                    else "SERVICE_CALL_OWNER_LOST_BEFORE_DISPATCH"
                )
                call.updated_at = datetime.now(UTC)
                if call.task_id:
                    task = await db.get(A2ATask, call.task_id)
                    if task and task.status in {"working", "submitted"}:
                        task.error = call.error
                        task.updated_at = call.updated_at
                        if not call.dispatched:
                            task.status = "failed"
                        await revoke_task_grants(db, task.id)
                await db.commit()
                return call.state
    except RuntimeErrorCode as exc:
        if exc.code == "RUNTIME_OPERATION_IN_PROGRESS":
            return "owned"
        raise


async def batch(cursor="", limit=32):
    from app.db.database import async_session

    async with async_session() as db:
        ids = (
            await db.scalars(
                select(ManagedServiceCall.id)
                .join(
                    ManagedServiceOwner,
                    ManagedServiceOwner.call_id == ManagedServiceCall.id,
                )
                .outerjoin(
                    ManagedServiceResult,
                    ManagedServiceResult.call_id == ManagedServiceCall.id,
                )
                .where(
                    ManagedServiceCall.scope == settings.runtime_scope,
                    ManagedServiceOwner.scope == settings.runtime_scope,
                    (
                        (ManagedServiceCall.state == "active")
                        | (
                            (ManagedServiceCall.state == "unknown")
                            & ManagedServiceResult.applied.is_(False)
                        )
                    ),
                    ManagedServiceCall.id > cursor,
                )
                .order_by(ManagedServiceCall.id)
                .limit(limit)
            )
        ).all()
    results = {}
    for call_id in ids:
        try:
            results["service:" + call_id] = await reconcile(call_id)
        except Exception as exc:  # noqa: BLE001 - one unavailable provider must not starve later calls
            results["service:" + call_id] = (
                exc.code if isinstance(exc, RuntimeErrorCode) else type(exc).__name__
            )
    return results, ids[-1] if len(ids) == limit else ""
