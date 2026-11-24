"""Persist received results before projecting them; replay storage, never the model."""

from datetime import UTC, datetime

from app.config import settings
from app.models.tables import (
    A2ATask,
    ChatMessage,
    ChatSession,
    ManagedServiceCall,
    ManagedServiceResult,
)
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import bounded, digest
from app.services import managed_admission


async def retain(call_id, kind, target_id, content, *, complete, evidence=None):
    from app.db.database import async_session

    if (
        kind not in {"task", "chat"}
        or not isinstance(content, str)
        or type(complete) is not bool
    ):
        raise RuntimeErrorCode("SERVICE_RESULT_INVALID")
    if complete and not content.strip():
        raise RuntimeErrorCode("SERVICE_RESULT_INCOMPLETE")
    record = {
        "call_id": call_id,
        "kind": kind,
        "target_id": target_id,
        "content": content,
        "complete": complete,
        "evidence": evidence or {},
    }
    bounded(record, 16 * 1024 * 1024)
    hashed = digest(record)
    async with async_session() as db:
        call = await db.get(ManagedServiceCall, call_id)
        if not call or call.scope != settings.runtime_scope or not call.dispatched:
            raise RuntimeErrorCode("SERVICE_CALL_NOT_DISPATCHED")
        await _target(db, call, record)
        old = await db.get(ManagedServiceResult, call_id)
        if old:
            if old.scope != settings.runtime_scope or old.digest != hashed:
                raise RuntimeErrorCode("SERVICE_RESULT_CONFLICT")
            return
        if call.state not in {"active", "unknown"}:
            raise RuntimeErrorCode("SERVICE_CALL_ALREADY_SETTLED")
        db.add(
            ManagedServiceResult(
                call_id=call_id,
                scope=settings.runtime_scope,
                record=record,
                digest=hashed,
            )
        )
        await db.commit()


async def _target(db, call, record):
    if record["kind"] == "task":
        target = await db.get(A2ATask, record["target_id"])
        if call.task_id != record["target_id"]:
            raise RuntimeErrorCode("SERVICE_RESULT_TARGET_MISMATCH")
    elif record["kind"] == "chat":
        target = await db.get(ChatSession, record["target_id"])
        if call.task_id:
            raise RuntimeErrorCode("SERVICE_RESULT_TARGET_MISMATCH")
    else:
        raise RuntimeErrorCode("SERVICE_RESULT_INVALID")
    if not target or target.agent_id != call.agent_id:
        raise RuntimeErrorCode("SERVICE_RESULT_TARGET_MISMATCH")
    return target


async def apply(call_id):
    """Caller holds the service-call native lock; all projection changes commit together."""
    from app.db.database import async_session
    from app.services.e2ag_tool_gateway import revoke_task_grants

    async with async_session() as db:
        call = await db.get(ManagedServiceCall, call_id)
        receipt = await db.get(ManagedServiceResult, call_id)
        if not call or call.scope != settings.runtime_scope:
            raise RuntimeErrorCode("SERVICE_CALL_NOT_FOUND")
        if not receipt:
            return None
        if (
            receipt.scope != settings.runtime_scope
            or digest(receipt.record) != receipt.digest
        ):
            raise RuntimeErrorCode("SERVICE_RESULT_CORRUPT")
        if receipt.applied:
            return call.state
        record = receipt.record
        if (
            record["call_id"] != call_id
            or not call.dispatched
            or call.state not in {"active", "unknown"}
        ):
            raise RuntimeErrorCode("SERVICE_RESULT_CONFLICT")
        target = await _target(db, call, record)
        if record["kind"] == "task":
            if target.status not in {"working", "submitted"}:
                raise RuntimeErrorCode("SERVICE_RESULT_TARGET_SETTLED")
            if record["content"]:
                target.artifacts = [
                    {
                        "artifactId": f"{call.task_id}-reply",
                        "name": "reply",
                        "parts": [{"kind": "text", "text": record["content"]}],
                        "metadata": {
                            "call_id": call_id,
                            "complete": record["complete"],
                        },
                    }
                ]
            target.status = "completed" if record["complete"] else "working"
            target.error = "" if record["complete"] else "SERVICE_RESULT_INCOMPLETE"
            target.updated_at = datetime.now(UTC)
            await revoke_task_grants(db, target.id)
        elif record["content"]:
            db.add(
                ChatMessage(
                    id=call_id,
                    session_id=target.id,
                    role="assistant",
                    content=record["content"],
                )
            )
        await managed_admission.finish_in_session(
            db,
            call_id,
            complete=record["complete"],
            error="" if record["complete"] else "SERVICE_RESULT_INCOMPLETE",
        )
        receipt.applied = True
        await db.commit()
        return call.state
