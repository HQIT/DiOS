"""Explicit operator-confirmed adoption of existing compatibility task runtimes."""

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.tables import (
    A2ATask,
    Agent,
    ManagedRuntimeControl,
    ManagedServiceCall,
    ManagedTaskAdoption,
    ManagedTaskExecution,
)
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import digest
from app.services import managed_runtime
from app.services.managed_files import read_file


def _identity(value, limit=128):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RuntimeErrorCode("INVALID_ADOPTION_REQUEST")
    return value


async def preview(task_id, native_id, workspace=None):
    from app.db.database import async_session
    from app.services.a2a_service import _extract_text_from_message

    _identity(task_id, 32)
    _identity(native_id, 64)
    async with async_session() as db:
        task = await db.get(A2ATask, task_id)
        if not task or task.status not in {"submitted", "working"}:
            raise RuntimeErrorCode("ADOPTION_TASK_NOT_ACTIVE")
        agent = await db.get(Agent, task.agent_id)
        if not agent:
            raise RuntimeErrorCode("ADOPTION_AGENT_NOT_FOUND")
        if await db.scalar(
            select(ManagedServiceCall.id)
            .where(ManagedServiceCall.task_id == task_id)
            .limit(1)
        ):
            raise RuntimeErrorCode("SERVICE_TASK_ADOPTION_UNSUPPORTED")
        run_id = task_id[:12]
        matches = (
            await db.scalars(select(A2ATask.id).where(A2ATask.id.startswith(run_id)))
        ).all()
        if sum(i[:12] == run_id for i in matches) != 1:
            raise RuntimeErrorCode("AMBIGUOUS_LEGACY_RUN_ID")
        root = Path(workspace or agent.workspace_path).absolute()
        if root != root.resolve() or not root.is_relative_to(
            settings.workspace_root.resolve()
        ):
            raise RuntimeErrorCode("INVALID_WORKSPACE_PATH")
        handle = managed_runtime.handle("task", task_id)
        execution = await db.get(ManagedTaskExecution, task_id)
        if execution and (
            execution.finished
            or execution.process_instance
            or execution.scope != settings.runtime_scope
            or execution.runtime_handle != handle
            or execution.workspace_path != str(root)
            or execution.run_id != run_id
        ):
            raise RuntimeErrorCode("MANAGED_EXECUTION_ALREADY_TRACKED")
        pinned_task = {
            k: getattr(task, k)
            for k in ("id", "agent_id", "status", "context_id", "trace_id", "message")
        }
        pinned_task["updated_at"] = (
            task.updated_at.isoformat() if task.updated_at else None
        )
        task_digest = digest(pinned_task)
        cancel_requested = bool(execution and execution.cancel_requested)
        expected_text = _extract_text_from_message(task.message)
    adapter = managed_runtime.managed_adapter()
    if not callable(getattr(adapter, "inspect_existing", None)):
        raise RuntimeErrorCode("RUNTIME_ADOPTION_UNSUPPORTED")
    snapshot = await adapter.inspect_existing(native_id)
    native = snapshot["identity"]
    if snapshot["observation"]["state"] not in {"running", "paused", "exited", "dead"}:
        raise RuntimeErrorCode("ADOPTION_RUNTIME_NOT_STARTED")
    legacy = (
        native.get("name") == "dios-run-" + run_id
        and native.get("run_id") == run_id
        and not native.get("managed_scope")
    )
    current = (
        native.get("name") == "dios-worker-" + handle
        and native.get("managed_scope") == getattr(adapter, "scope", None)
        and native.get("kind") == "task"
    )
    if (
        not (legacy or current)
        or native.get("task_config") != f"/workspace/agent-task-{run_id}.json"
    ):
        raise RuntimeErrorCode("ADOPTION_TASK_RUNTIME_MISMATCH")
    mount = native.get("workspace", {})
    if mount != {
        "Type": "bind",
        "Source": managed_runtime.host_workspace(root),
        "RW": True,
    }:
        raise RuntimeErrorCode("ADOPTION_WORKSPACE_MISMATCH")
    raw = await asyncio.to_thread(read_file, root, f"agent-task-{run_id}.json")
    config = json.loads(raw)
    section = config.get("task", {}) if isinstance(config, dict) else {}
    if (
        not isinstance(section, dict)
        or section.get("task") != expected_text
        or section.get("output_dir") != f"output/events/{run_id}"
    ):
        raise RuntimeErrorCode("ADOPTION_TASK_CONFIG_MISMATCH")
    record = {
        "scope": settings.runtime_scope,
        "task_id": task_id,
        "task_digest": task_digest,
        "runtime_handle": handle,
        "workspace_path": str(root),
        "run_id": run_id,
        "config_digest": hashlib.sha256(raw).hexdigest(),
        "identity": native,
        "cancel_requested": cancel_requested,
        "source": "legacy" if legacy else "managed",
    }
    return {
        "record": record,
        "digest": digest(record),
        "observation": snapshot["observation"],
        "source_quiescence_required": "operator_attestation",
        "new_grants": False,
    }


async def adopt(
    task_id,
    native_id,
    expected_digest,
    *,
    actor,
    reason,
    source_quiesced,
    workspace=None,
):
    from app.db.database import async_session
    from app.services.e2ag_tool_gateway import revoke_task_grants
    from app.services.managed_recovery import controller, register

    _identity(task_id, 32)
    _identity(native_id, 64)
    _identity(actor)
    _identity(reason, 2000)
    if source_quiesced is not True or not re.fullmatch("[0-9a-f]{64}", expected_digest):
        raise RuntimeErrorCode("ADOPTION_CONFIRMATION_REQUIRED")
    handle = managed_runtime.handle("task", task_id)
    async with (
        managed_runtime.serialized("admission:" + settings.runtime_scope),
        managed_runtime.exclusive(handle),
        managed_runtime.exclusive("adopt:" + native_id),
    ):
        async with async_session() as db:
            old = await db.get(ManagedTaskAdoption, task_id)
            if old:
                if (
                    old.scope != settings.runtime_scope
                    or old.native_id != native_id
                    or old.digest != expected_digest
                ):
                    raise RuntimeErrorCode("ADOPTION_CONFLICT")
                return {
                    "task_id": task_id,
                    "digest": old.digest,
                    "adopted": True,
                    "repeated": True,
                }
            control = await db.get(ManagedRuntimeControl, settings.runtime_scope)
            if not control or control.mode != "draining":
                raise RuntimeErrorCode("ADOPTION_DRAIN_REQUIRED")
        plan = await preview(task_id, native_id, workspace)
        if plan["digest"] != expected_digest:
            raise RuntimeErrorCode("ADOPTION_PREVIEW_CHANGED")
        record = plan["record"]
        async with async_session() as db:
            execution = await register(
                db, task_id, handle, record["workspace_path"], record["run_id"]
            )
            execution.process_instance = record["identity"]["process_instance"]
            execution.observation = {**plan["observation"], "handle": handle}
            execution.observed_at = datetime.now(UTC)
            execution.error = ""
            task = await db.get(A2ATask, task_id)
            task.status = "working"
            await revoke_task_grants(db, task_id)
            if task.context_id:
                from app.models.tables import EventLog
                from app.services.e2ag import append_audit_entry, verify_audit_chain

                event = await db.get(EventLog, task.context_id)
                if event:
                    if not verify_audit_chain(event.audit_chain or []):
                        raise RuntimeErrorCode("ADOPTION_AUDIT_INVALID")
                    event.audit_chain = append_audit_entry(
                        event.audit_chain or [],
                        trace_id=event.trace_id,
                        stage="runtime_adoption",
                        outcome="adopted",
                        evidence={
                            "task_id": task_id,
                            "task_trace_id": task.trace_id,
                            "native_id": native_id,
                            "preview_digest": expected_digest,
                            "actor": actor,
                            "reason": reason,
                            "source_quiescence": "operator_attested",
                            "new_grants": False,
                        },
                    )
            db.add(
                ManagedTaskAdoption(
                    task_id=task_id,
                    scope=settings.runtime_scope,
                    native_id=native_id,
                    record=record,
                    digest=expected_digest,
                    actor=actor,
                    reason=reason,
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                raise RuntimeErrorCode("ADOPTION_CONFLICT") from None
        controller.wake()
        return {
            "task_id": task_id,
            "digest": expected_digest,
            "adopted": True,
            "repeated": False,
            "new_grants": False,
            "source_quiescence": "operator_attested",
        }


async def adapter_for(db, task_id):
    adapter = managed_runtime.managed_adapter()
    adoption = await db.get(ManagedTaskAdoption, task_id)
    if adoption is None:
        return adapter
    record = adoption.record
    if (
        adoption.scope != settings.runtime_scope
        or digest(record) != adoption.digest
        or record.get("task_id") != task_id
        or record.get("scope") != settings.runtime_scope
    ):
        raise RuntimeErrorCode("ADOPTION_RECORD_INVALID")
    execution = await db.get(ManagedTaskExecution, task_id)
    if (
        adoption.native_id != record["identity"]["native_id"]
        or not execution
        or execution.process_instance != record["identity"]["process_instance"]
        or any(
            getattr(execution, key) != record[key]
            for key in ("scope", "runtime_handle", "workspace_path", "run_id")
        )
    ):
        raise RuntimeErrorCode("ADOPTION_EXECUTION_MISMATCH")
    if (
        not callable(getattr(adapter, "bind_existing", None))
        or adapter.name != record["identity"]["provider"]
    ):
        raise RuntimeErrorCode("RUNTIME_ADOPTION_UNSUPPORTED")
    return adapter.bind_existing(record["runtime_handle"], record["identity"])


async def main(args):
    if args.command == "preview":
        return await preview(args.task_id, args.native_id, args.workspace)
    return await adopt(
        args.task_id,
        args.native_id,
        args.expected_digest,
        actor=args.actor,
        reason=args.reason,
        source_quiesced=args.source_quiesced,
        workspace=args.workspace,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "adopt"):
        command = commands.add_parser(name)
        command.add_argument("task_id")
        command.add_argument("native_id")
        command.add_argument("--workspace")
        if name == "adopt":
            for key in ("expected-digest", "actor", "reason"):
                command.add_argument("--" + key, required=True)
            command.add_argument("--source-quiesced", action="store_true")
    try:
        print(json.dumps(asyncio.run(main(parser.parse_args())), ensure_ascii=False))
    except (RuntimeErrorCode, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "error": exc.code
                    if isinstance(exc, RuntimeErrorCode)
                    else "ADOPTION_SOURCE_INVALID"
                }
            )
        )
        raise SystemExit(2) from None
