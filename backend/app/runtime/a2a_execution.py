"""Durable remote intents and authenticated observations in the existing Runtime DB."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from a2a.types import Message, Task
from filelock import FileLock
from sqlalchemy import func, select

from .database import RuntimeErrorCode
from .lifecycle import admission
from .result_contract import validate_bytes
from .schema import artifacts, attempts, runs
from .store import (
    ACTIVE_ATTEMPTS,
    TERMINAL,
    RuntimeStore,
    bounded,
    digest,
    encoded,
    identifier,
)
from .work_authorizations import require_current

REMOTE_TERMINAL = {"completed", "failed", "rejected", "canceled"}


class A2AReceipts:
    def __init__(self, provider, scope):
        self.root = provider.root / (".a2a-" + digest(scope))
        self.root.mkdir(mode=0o700, exist_ok=True)

    def path(self, attempt_id):
        return self.root / digest(attempt_id)

    def save(self, run, attempt, spec, task):
        value = {
            "run_id": run["id"],
            "attempt_id": attempt["id"],
            "epoch": attempt["epoch"],
            "profile_digest": spec.fingerprint(),
            "request_digest": attempt["remote"]["a2a"]["request_digest"],
            "task": task.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
        raw = encoded(value)
        if len(raw) > spec.response_limit + 4096:
            raise RuntimeErrorCode("A2A_RESPONSE_TOO_LARGE")
        target = self.path(attempt["id"])
        with FileLock(str(target) + ".lock", timeout=5):
            if target.exists():
                prior = json.loads(target.read_bytes())
                if (prior["task"]["id"], prior["task"]["contextId"]) != (
                    task.id,
                    task.context_id,
                ):
                    raise RuntimeErrorCode("A2A_TASK_BINDING_CHANGED")
                if prior["task"]["status"]["state"] in REMOTE_TERMINAL:
                    return prior
            fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                if os.name != "nt":
                    directory = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return value

    def load(self, run, attempt, spec):
        path = self.path(attempt["id"])
        if not path.exists():
            return None
        if path.stat().st_size > spec.response_limit + 4096:
            raise RuntimeErrorCode("A2A_RESPONSE_TOO_LARGE")
        value = json.loads(path.read_bytes())
        expected = {
            "run_id": run["id"],
            "attempt_id": attempt["id"],
            "epoch": attempt["epoch"],
            "profile_digest": spec.fingerprint(),
            "request_digest": attempt["remote"]["a2a"]["request_digest"],
        }
        if any(value.get(k) != v for k, v in expected.items()):
            raise RuntimeErrorCode("A2A_RECEIPT_BINDING_CHANGED")
        return Task.model_validate(value["task"])


class A2AExecution:
    def __init__(self, store, provider, spec):
        self.store, self.provider, self.spec = store, provider, spec
        self.receipts = A2AReceipts(provider, store.scope)

    async def claim(self, worker_id, instance, profile_id, revision_ref=None):
        from .remote_control import TransactionView

        async with self.store.db.transaction(self.store.scope) as c:
            count = (
                await c.execute(
                    select(func.count())
                    .select_from(attempts.join(runs, runs.c.id == attempts.c.run_id))
                    .where(
                        runs.c.scope == self.store.scope,
                        runs.c.input["runtime_profile"].as_string() == profile_id,
                        attempts.c.state.in_(ACTIVE_ATTEMPTS | {"result_unknown"}),
                    )
                )
            ).scalar_one()
            if count >= self.spec.max_concurrent:
                return None
            bound = RuntimeStore(
                TransactionView(self.store.db, c, self.store.scope), self.store.scope
            )
            return await bound.claim(
                worker_id,
                instance,
                profile_id=profile_id,
                profile_digest=self.spec.fingerprint(),
                revision_ref=revision_ref,
                recovery_policy=self.spec.recovery_policy(),
            )

    async def prepare(self, binding):
        async with self.store.db.transaction(self.store.scope) as c:
            run, attempt, now = await self.store._owned(c, *binding, authorize=True)
            if run["input"].get("profile_digest") != self.spec.fingerprint():
                raise RuntimeErrorCode("PROFILE_CHANGED")
            if not run["input"].get("work_authorization_ref"):
                raise RuntimeErrorCode("WORK_AUTHORIZATION_REQUIRED")
            if run["input"].get("input_registration", {}).get("result_schema") is None:
                raise RuntimeErrorCode("A2A_RESULT_SCHEMA_REQUIRED")
            message = Message.model_validate(
                {
                    "role": "user",
                    "messageId": "dios-"
                    + digest([self.store.scope, run["id"], attempt["id"]]),
                    "parts": run["input"]["payload"]["sample"]["parts"],
                }
            )
            if not message.parts:
                raise RuntimeErrorCode("A2A_MESSAGE_REQUIRED")
            value = {
                "protocol": "0.3.0",
                "profile_digest": self.spec.fingerprint(),
                "endpoint": self.spec.endpoint,
                "request_digest": digest(message.model_dump(mode="json")),
                "message": message.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
                "stage": "prepared",
                "task_id": None,
                "context_id": None,
                "observed_state": None,
                "response_digest": None,
                "observed_at": None,
                "cancel_requested_at": None,
            }
            bounded(value)
            if "a2a" in attempt["remote"]:
                old = attempt["remote"]["a2a"]
                if old["request_digest"] != value["request_digest"]:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return old
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(state="preparing", remote={"a2a": value})
            )
            await self.store._event(
                c,
                run,
                "a2a.intent_prepared",
                {
                    "attempt_id": attempt["id"],
                    "request_digest": value["request_digest"],
                },
                now,
            )
            return value

    async def dispatch(self, binding):
        async with self.store.db.transaction(self.store.scope) as c:
            run, attempt, now = await self.store._owned(c, *binding, authorize=True)
            value = attempt["remote"]["a2a"]
            if value["stage"] != "prepared":
                raise RuntimeErrorCode("A2A_SUBMISSION_ALREADY_DISPATCHED")
            value = {**value, "stage": "dispatched", "dispatched_at": now}
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(
                    state="creating_session", remote={**attempt["remote"], "a2a": value}
                )
            )
            await self.store._event(
                c, run, "a2a.submission_dispatched", {"attempt_id": attempt["id"]}, now
            )
            return value["message"]

    async def checked(self, c, expected_run, expected_attempt, *, reconcile=False):
        await admission(c, self.store.scope, ownership=True)
        run = await self.store._run(c, expected_run["id"])
        if run["version"] != expected_run["version"]:
            raise RuntimeErrorCode("VERSION_CONFLICT")
        attempt = (
            (
                await c.execute(
                    select(attempts).where(attempts.c.id == run["attempt_id"])
                )
            )
            .mappings()
            .first()
        )
        if (
            not attempt
            or attempt["id"] != expected_attempt["id"]
            or run["state"] in TERMINAL
        ):
            raise RuntimeErrorCode("STALE_OWNER")
        if reconcile:
            if (
                run["state"] != "reconciliation_required"
                or attempt["state"] != "result_unknown"
                or run["epoch"] != attempt["epoch"] + 1
            ):
                raise RuntimeErrorCode("STALE_OWNER")
        else:
            await self.store._owned(
                c,
                run["id"],
                attempt["id"],
                attempt["worker_id"],
                attempt["worker_instance"],
                attempt["epoch"],
            )
        if (
            run["input"].get("profile_digest") != self.spec.fingerprint()
            or attempt["remote"].get("a2a", {}).get("profile_digest")
            != self.spec.fingerprint()
        ):
            raise RuntimeErrorCode("PROFILE_CHANGED")
        return run, dict(attempt), await self.store.db.now(c)

    async def blob(self, c, run, attempt, blob, kind, now):
        old = (
            await c.execute(
                select(artifacts.c.id).where(
                    artifacts.c.attempt_id == attempt["id"],
                    artifacts.c.digest == blob.digest,
                )
            )
        ).scalar_one_or_none()
        if old:
            return old
        aid = str(uuid4())
        await c.execute(
            artifacts.insert().values(
                id=aid,
                run_id=run["id"],
                attempt_id=attempt["id"],
                digest=blob.digest,
                size=blob.size,
                provider=self.provider.name,
                storage_key=blob.key,
                created_at=now,
            )
        )
        return aid

    async def observe(self, expected_run, expected_attempt, task, *, reconcile=False):
        identifier(task.id)
        identifier(task.context_id)
        raw = encoded(task.model_dump(mode="json", by_alias=True, exclude_none=True))
        blob = await asyncio.to_thread(self.provider.put, [raw])
        async with self.store.db.transaction(self.store.scope) as c:
            run, attempt, now = await self.checked(
                c, expected_run, expected_attempt, reconcile=reconcile
            )
            value = attempt["remote"]["a2a"]
            if value["task_id"] is not None and (
                value["task_id"],
                value["context_id"],
            ) != (task.id, task.context_id):
                raise RuntimeErrorCode("A2A_TASK_BINDING_CHANGED")
            if (
                value["observed_state"] in REMOTE_TERMINAL
                and task.status.state != value["observed_state"]
            ):
                raise RuntimeErrorCode("A2A_TASK_STATE_REGRESSION")
            aid = await self.blob(c, run, attempt, blob, "a2a-response", now)
            changed = value["response_digest"] != blob.digest
            value = {
                **value,
                "stage": "bound",
                "task_id": task.id,
                "context_id": task.context_id,
                "observed_state": task.status.state.value,
                "response_digest": blob.digest,
                "response_artifact_id": aid,
                "observed_at": now,
            }
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(
                    session_id=task.context_id,
                    remote={**attempt["remote"], "a2a": value},
                    state="result_unknown"
                    if run["state"] == "reconciliation_required"
                    else "waiting_model",
                )
            )
            if changed:
                await self.store._event(
                    c,
                    run,
                    "a2a.observed",
                    {
                        "attempt_id": attempt["id"],
                        "task_id": task.id,
                        "context_id": task.context_id,
                        "state": task.status.state.value,
                        "artifact_id": aid,
                    },
                    now,
                )

    async def cancel_intent(
        self,
        expected_run,
        expected_attempt,
        reason="CANCEL_REQUESTED",
        *,
        reconcile=False,
    ):
        async with self.store.db.transaction(self.store.scope) as c:
            run, attempt, now = await self.checked(
                c, expected_run, expected_attempt, reconcile=reconcile
            )
            value = attempt["remote"]["a2a"]
            if not value["task_id"]:
                raise RuntimeErrorCode("A2A_REMOTE_ID_UNKNOWN")
            if value["cancel_requested_at"] is None:
                await c.execute(
                    attempts.update()
                    .where(attempts.c.id == attempt["id"])
                    .values(
                        remote={
                            **attempt["remote"],
                            "a2a": {
                                **value,
                                "cancel_requested_at": now,
                                "cancel_reason": reason,
                            },
                        }
                    )
                )
                await self.store._event(
                    c, run, "a2a.cancel_requested", {"task_id": value["task_id"]}, now
                )

    async def abort_prepared(self, expected_run, expected_attempt):
        async with self.store.db.transaction(self.store.scope) as c:
            run, attempt, now = await self.checked(
                c, expected_run, expected_attempt, reconcile=True
            )
            if attempt["remote"]["a2a"]["stage"] != "prepared":
                raise RuntimeErrorCode("A2A_SUBMISSION_ALREADY_DISPATCHED")
            state = "cancelled" if run["cancel_requested"] else "failed"
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(
                    state=state,
                    finished_at=now,
                    error={"code": "A2A_SUBMISSION_NOT_DISPATCHED"},
                )
            )
            await self.store._event(
                c,
                run,
                "a2a.not_dispatched",
                {"attempt_id": attempt["id"]},
                now,
                state=state,
            )

    async def finish(
        self,
        expected_run,
        expected_attempt,
        task,
        *,
        data=None,
        failure=None,
        reconcile=False,
    ):
        if task.status.state not in REMOTE_TERMINAL:
            raise RuntimeErrorCode("A2A_TERMINAL_NOT_CONFIRMED")
        blob = None
        if data is not None:
            validate_bytes(expected_run["input"], data)
            blob = await asyncio.to_thread(self.provider.put, [data])
        async with self.store.db.transaction(self.store.scope) as c:
            run, attempt, now = await self.checked(
                c, expected_run, expected_attempt, reconcile=reconcile
            )
            value = attempt["remote"]["a2a"]
            if (value["task_id"], value["context_id"], value["observed_state"]) != (
                task.id,
                task.context_id,
                task.status.state.value,
            ):
                raise RuntimeErrorCode("A2A_TASK_BINDING_CHANGED")
            if (
                run["revoked"]
                or run["authorization_epoch"] != attempt["authorization_epoch"]
            ):
                failure = "AUTH_REVOKED"
            if run["cancel_requested"]:
                state = "cancelled" if task.status.state == "canceled" else "failed"
                failure = None if state == "cancelled" else "A2A_CANCEL_TOO_LATE"
            elif failure:
                state = "failed"
            elif task.status.state == "completed" and blob:
                await require_current(
                    c, run, attempt, now, "accept_result", session_id=task.context_id
                )
                from .tool_invocations import require_settled

                await require_settled(c, self.store.scope, run["id"])
                state = "succeeded"
            else:
                state, failure = (
                    "failed",
                    "A2A_REMOTE_" + task.status.state.value.upper(),
                )
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(
                    state="completed" if state == "succeeded" else state,
                    finished_at=now,
                    error={"code": failure} if failure else None,
                )
            )
            detail = {
                "attempt_id": attempt["id"],
                "remote_task_id": task.id,
                "response_artifact_id": value["response_artifact_id"],
            }
            if state == "succeeded":
                detail["artifact_id"] = await self.blob(
                    c, run, attempt, blob, "result", now
                )
            if failure:
                detail["code"] = failure
            await self.store._event(
                c,
                run,
                "run.completed" if state == "succeeded" else "a2a.finished",
                detail,
                now,
                state=state,
            )
