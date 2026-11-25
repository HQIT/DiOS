"""Persistent compatibility admission, serialized by the configured native host lock."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.config import settings
from app.models.tables import (
    A2ATask,
    ManagedRuntimeControl,
    ManagedServiceCall,
    ManagedServiceOwner,
)
from app.runtime.database import RuntimeErrorCode
from app.services import managed_runtime

_owner = ContextVar("managed_admission_owner", default=None)


async def restoring(db):
    held = await db.scalar(
        select(ManagedRuntimeControl.scope)
        .where(ManagedRuntimeControl.mode == "restore_hold")
        .limit(1)
    )
    return held is not None


async def assert_not_restoring(db):
    if await restoring(db):
        raise RuntimeErrorCode("RESTORE_RECONCILIATION_REQUIRED")


@asynccontextmanager
async def gate(db, *, admitted_task_id=None):
    await assert_not_restoring(db)
    # Nested calls in the same fan-out retain one admission. Spawned tasks must
    # acquire independently, even though asyncio copies their context.
    if _owner.get() is asyncio.current_task():
        yield
        return
    async with managed_runtime.serialized("admission:" + settings.runtime_scope):
        control = await db.get(
            ManagedRuntimeControl, settings.runtime_scope, populate_existing=True
        )
        if control and control.mode != "running":
            task = await db.get(A2ATask, admitted_task_id) if admitted_task_id else None
            if not task or task.status not in {"submitted", "working"}:
                raise RuntimeErrorCode("RUNTIME_DRAINING")
        token = _owner.set(asyncio.current_task())
        try:
            yield
        finally:
            _owner.reset(token)


async def begin_call(db, agent_id, *, task_id=None, owner_id=None):
    if task_id:
        task = await db.get(A2ATask, task_id)
        if (
            not task
            or task.agent_id != agent_id
            or task.status not in {"submitted", "working"}
        ):
            raise RuntimeErrorCode("SERVICE_CALL_TASK_MISMATCH")
    async with gate(db, admitted_task_id=task_id):
        if task_id:
            existing = await db.scalar(
                select(ManagedServiceCall.id)
                .where(
                    ManagedServiceCall.scope == settings.runtime_scope,
                    ManagedServiceCall.task_id == task_id,
                )
                .limit(1)
            )
            if existing:
                raise RuntimeErrorCode("SERVICE_CALL_ALREADY_EXISTS")
        call = ManagedServiceCall(
            id=owner_id or uuid4().hex,
            scope=settings.runtime_scope,
            agent_id=agent_id,
            task_id=task_id,
        )
        db.add(call)
        if owner_id:
            db.add(ManagedServiceOwner(call_id=call.id, scope=settings.runtime_scope))
        await db.commit()
    return call.id


async def validate_call(db, call_id, agent_id):
    await assert_not_restoring(db)
    call = await db.get(ManagedServiceCall, call_id)
    if (
        not call
        or call.scope != settings.runtime_scope
        or call.agent_id != agent_id
        or call.state != "active"
    ):
        raise RuntimeErrorCode("SERVICE_CALL_NOT_ACTIVE")
    return call


async def dispatch(call_id):
    from app.db.database import async_session

    async with async_session() as db:
        await assert_not_restoring(db)
        call = await db.get(ManagedServiceCall, call_id)
        if not call or call.scope != settings.runtime_scope or call.state != "active":
            raise RuntimeErrorCode("SERVICE_CALL_NOT_ACTIVE")
        call.dispatched = True
        call.updated_at = datetime.now(UTC)
        await db.commit()


async def finish(call_id, *, complete=False, error=""):
    from app.db.database import async_session

    async with async_session() as db:
        await finish_in_session(db, call_id, complete=complete, error=error)
        await db.commit()


async def finish_in_session(db, call_id, *, complete=False, error=""):
    """The caller commits the result and its completion marker in one transaction."""
    call = await db.get(ManagedServiceCall, call_id, populate_existing=True)
    if not call or call.scope != settings.runtime_scope:
        raise RuntimeErrorCode("SERVICE_CALL_NOT_FOUND")
    if call.state not in {"completed", "failed"}:
        call.state = (
            "completed" if complete else "unknown" if call.dispatched else "failed"
        )
        call.error = error
        call.updated_at = datetime.now(UTC)
    return call
