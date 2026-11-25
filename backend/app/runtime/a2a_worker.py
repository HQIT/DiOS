"""Remote A2A execution and observation; never invents remote process evidence."""

import asyncio
import os
import signal
import socket
import time
from pathlib import Path
from uuid import uuid4

from a2a.client.errors import A2AClientJSONRPCError
from sqlalchemy import or_, select

from .a2a_execution import REMOTE_TERMINAL, A2AExecution
from .a2a_faults import A2AFault
from .a2a_faults import code as error_code
from .a2a_recovery import A2ARecovery
from .a2a_transport import A2ATransport
from .authorization import authorize_work
from .database import RuntimeErrorCode
from .principals import admit
from .schema import runs
from .store import TERMINAL
from .work_authorizations import WorkAuthorizationVerifier, checked_work
from .work_policy import policy_matches


class A2AWorker:
    def __init__(
        self,
        store,
        profile_id,
        spec,
        provider,
        policy,
        *,
        secret_resolver=None,
        secret_requests=(),
        timeout=300,
        poll_interval=1,
        transport_factory=A2ATransport,
        revision_ref=None,
    ):
        if policy is None:
            raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
        self.revision_ref = revision_ref
        spec.validate()
        if not 0 < timeout <= 86400 or not 0.05 <= poll_interval <= 5:
            raise RuntimeErrorCode("INVALID_WORKER_LIMIT")
        self.store, self.profile_id, self.spec, self.provider, self.policy = (
            store,
            profile_id,
            spec,
            provider,
            policy,
        )
        self.secret_resolver, self.secret_requests = (
            secret_resolver,
            list(secret_requests),
        )
        if len(self.secret_requests) > 1:
            raise RuntimeErrorCode("A2A_SINGLE_CREDENTIAL_REQUIRED")
        self.timeout, self.poll_interval, self.transport_factory = (
            timeout,
            poll_interval,
            transport_factory,
        )
        self.worker_id, self.instance = str(uuid4()), str(uuid4())
        self.execution = A2AExecution(store, provider, spec)
        self.recovery = A2ARecovery(self.execution)
        self.verifier = WorkAuthorizationVerifier(store, policy)
        self.stopping = False

    async def transport(self):
        token = None
        if self.secret_requests:
            if self.secret_resolver is None:
                raise RuntimeErrorCode("SECRET_AUTHORIZATION_REQUIRED")
            mounts = await asyncio.to_thread(
                self.secret_resolver.resolve, self.profile_id, self.secret_requests
            )
            token = (
                await asyncio.to_thread(
                    Path(mounts[0].source).read_text, encoding="utf-8"
                )
            ).strip()
        return self.transport_factory(self.spec, token=token)

    async def snapshot(self, run_id):
        run = await self.store.get(run_id)
        attempt = next(
            (a for a in run["attempts"] if a["id"] == run["attempt_id"]), None
        )
        return run, attempt

    @staticmethod
    def binding(run, attempt):
        return (
            run["id"],
            attempt["id"],
            attempt["worker_id"],
            attempt["worker_instance"],
            attempt["epoch"],
        )

    async def authority(self, run, attempt):
        if run["state"] != "reconciliation_required":
            await authorize_work(
                self.store,
                run,
                attempt,
                "execute",
                session_id=attempt["session_id"],
                verifier=self.verifier,
            )
            await self.store.heartbeat(*self.binding(run, attempt))
            return
        # Read-only recovery checks the original Work, including live identity,
        # deadline and delegation. It never renews or restores the expired lease.
        current = await asyncio.to_thread(
            admit,
            self.policy,
            self.profile_id,
            self.spec.fingerprint(),
            run["input"].get("secret_refs", []),
            run["input"].get("principal"),
        )
        if not policy_matches(current, run["input"]["work_policy_digest"]):
            raise RuntimeErrorCode("WORK_POLICY_CHANGED")
        async with self.store.db.read(self.store.scope) as c:
            await checked_work(c, run, attempt, await self.store.db.now(c), "execute")

    async def retain(self, run, attempt, task, *, reconcile=False):
        receipt = await asyncio.to_thread(
            self.execution.receipts.save, run, attempt, self.spec, task
        )
        from a2a.types import Task

        task = Task.model_validate(receipt["task"])
        # A cancellation may race receipt persistence. Re-read only the CAS
        # snapshot; the invocation identity is still checked by the repository.
        current, a = await self.snapshot(run["id"])
        if not a or a["id"] != attempt["id"] or a["epoch"] != attempt["epoch"]:
            raise RuntimeErrorCode("STALE_OWNER")
        if current["state"] not in TERMINAL:
            await self.execution.observe(current, a, task, reconcile=reconcile)
        return task

    async def tick(self, run_id, *, reconcile=False):
        run, attempt = await self.snapshot(run_id)
        if run["state"] in TERMINAL:
            return run
        if not reconcile and (
            run["state"] == "reconciliation_required"
            or attempt["worker_id"] != self.worker_id
            or attempt["worker_instance"] != self.instance
        ):
            raise RuntimeErrorCode("STALE_OWNER")
        remote = attempt["remote"]["a2a"]
        if remote["profile_digest"] != self.spec.fingerprint():
            raise RuntimeErrorCode("PROFILE_CHANGED")
        receipt = await asyncio.to_thread(
            self.execution.receipts.load, run, attempt, self.spec
        )
        if receipt is not None and (
            not remote["task_id"] or receipt.status.state in REMOTE_TERMINAL
        ):
            await self.execution.observe(run, attempt, receipt, reconcile=reconcile)
            run, attempt = await self.snapshot(run_id)
            remote = attempt["remote"]["a2a"]
        if not remote["task_id"]:
            if (
                remote["stage"] == "prepared"
                and run["state"] == "reconciliation_required"
            ):
                await self.execution.abort_prepared(run, attempt)
            elif reconcile:
                await self.recovery.failure(
                    run,
                    attempt,
                    RuntimeErrorCode("A2A_REMOTE_ID_UNKNOWN"),
                    reconcile=True,
                )
            return (await self.snapshot(run_id))[0]
        reason = remote.get("cancel_reason")
        if not reason:
            try:
                await self.authority(run, attempt)
            except RuntimeErrorCode as exc:
                if exc.code in {"STALE_OWNER", "VERSION_CONFLICT"}:
                    raise
                reason = exc.code
        token = await self.recovery.permit(run, attempt, reconcile=reconcile)
        if token is None:
            if not reconcile:
                await self.store.mark_unknown(
                    self.binding(run, attempt), "A2A_OBSERVATION_COOLDOWN"
                )
            return (await self.snapshot(run_id))[0]
        transport = None
        try:
            transport = await self.transport()
            if reason and remote["observed_state"] not in REMOTE_TERMINAL:
                await self.execution.cancel_intent(
                    run, attempt, reason, reconcile=reconcile
                )
                try:
                    await transport.cancel(remote["task_id"])
                except A2AClientJSONRPCError:
                    pass  # A task may finish before cancel; query the original id.
            task = (
                receipt
                if receipt is not None and receipt.status.state in REMOTE_TERMINAL
                else await transport.observe(remote["task_id"])
            )
            task = await self.retain(run, attempt, task, reconcile=reconcile)
            if task.status.state not in REMOTE_TERMINAL:
                await self.recovery.success(run, attempt, token, reconcile=reconcile)
                return (await self.snapshot(run_id))[0]
            run, attempt = await self.snapshot(run_id)
            failure, data = reason, None
            if task.status.state == "completed" and not reason:
                try:
                    await authorize_work(
                        self.store,
                        run,
                        attempt,
                        "accept_result",
                        session_id=task.context_id,
                        verifier=self.verifier,
                    )
                except RuntimeErrorCode as exc:
                    failure = exc.code
                if not failure:
                    # Transfer failures are observation failures, not completed
                    # tasks; preserve the remote result and retry its download.
                    from .result_contract import validate_bytes

                    try:
                        data = await transport.result(task)
                        validate_bytes(run["input"], data)
                    except A2AFault:
                        raise
                    except RuntimeErrorCode as exc:
                        failure, data = exc.code, None
            await self.recovery.success(run, attempt, token, reconcile=reconcile)
            run, attempt = await self.snapshot(run_id)
            await self.execution.finish(
                run, attempt, task, data=data, failure=failure, reconcile=reconcile
            )
            return (await self.snapshot(run_id))[0]
        except Exception as exc:
            if isinstance(exc, RuntimeErrorCode) and exc.code in {
                "STALE_OWNER",
                "VERSION_CONFLICT",
            }:
                if exc.code == "VERSION_CONFLICT":
                    await self.recovery.release(
                        run, attempt, token, reconcile=reconcile
                    )
                raise
            await self.recovery.failure(
                run, attempt, exc, token=token, reconcile=reconcile
            )
            return (await self.snapshot(run_id))[0]
        finally:
            if transport:
                await transport.aclose()

    async def uncertain(self, run_id, exc):
        run, attempt = await self.snapshot(run_id)
        if run["state"] not in TERMINAL | {"reconciliation_required"}:
            try:
                await self.recovery.failure(run, attempt, exc)
            except RuntimeErrorCode as error:
                if error.code not in {"STALE_OWNER", "VERSION_CONFLICT"}:
                    raise

    async def once(self):
        await self.store.register_worker(
            self.worker_id, self.instance, socket.gethostname(), os.getpid()
        )
        attempt = await self.execution.claim(
            self.worker_id, self.instance, self.profile_id, self.revision_ref
        )
        if attempt is None:
            return {"worker_id": self.worker_id, "state": "idle"}
        run, attempt = await self.snapshot(attempt["run_id"])
        try:
            if run["input"].get("profile_digest") != self.spec.fingerprint():
                raise RuntimeErrorCode("PROFILE_CHANGED")
            if run["input"].get("secret_refs", []) != self.secret_requests:
                raise RuntimeErrorCode("SECRET_REFERENCE_CHANGED")
            await self.authority(run, attempt)
            transport = await self.transport()
            try:
                declaration = None
                if self.revision_ref:
                    from .endpoint_compatibility import Compatibility

                    declaration = await Compatibility(
                        self.store.db, self.store.scope
                    ).discover(transport, self.spec)
                else:
                    await transport.discover()
            finally:
                await transport.aclose()
            await self.execution.prepare(
                self.binding(run, attempt),
                declaration_digest=declaration["snapshot_digest"]
                if declaration
                else None,
            )
        except Exception as exc:  # noqa: BLE001 - retain sanitized preflight failure
            await self.recovery.preflight_failed(run, attempt, exc)
            return (await self.snapshot(run["id"]))[0]
        try:
            transport = await self.transport()
            try:
                message = await self.execution.dispatch(self.binding(run, attempt))
                task = await transport.submit(message)
                run, attempt = await self.snapshot(run["id"])
                await self.retain(run, attempt, task)
            finally:
                await transport.aclose()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    current = await self.tick(run["id"])
                except RuntimeErrorCode as exc:
                    if exc.code != "VERSION_CONFLICT":
                        raise
                    await asyncio.sleep(self.poll_interval)
                    continue
                if current["state"] in TERMINAL | {"reconciliation_required"}:
                    return current
                await asyncio.sleep(self.poll_interval)
            raise RuntimeErrorCode("A2A_OBSERVATION_DEADLINE")
        except Exception as exc:  # noqa: BLE001 - uncertainty never permits blind redispatch
            await self.uncertain(run["id"], exc)
            return (await self.snapshot(run["id"]))[0]

    async def recover(self):
        await self.store.expire()
        from .fault_schema import plans

        async with self.store.db.read(self.store.scope) as c:
            now = await self.store.db.now(c)
            ids = (
                (
                    await c.execute(
                        select(runs.c.id)
                        .outerjoin(plans, plans.c.run_id == runs.c.id)
                        .where(
                            runs.c.scope == self.store.scope,
                            runs.c.state == "reconciliation_required",
                            runs.c.input["runtime_profile"].as_string()
                            == self.profile_id,
                            runs.c.input["revision_ref"].as_string()
                            == self.revision_ref
                            if self.revision_ref is not None
                            else runs.c.input["revision_ref"].as_string().is_(None),
                            or_(plans.c.retry_at.is_(None), plans.c.retry_at <= now),
                        )
                        .order_by(runs.c.updated_at)
                        .limit(32)
                    )
                )
                .scalars()
                .all()
            )
        result = []
        for run_id in ids:
            try:
                result.append(
                    {
                        "run_id": run_id,
                        "state": (await self.tick(run_id, reconcile=True))["state"],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one unavailable peer cannot stop reconciliation
                result.append({"run_id": run_id, "error": error_code(exc)})
        return result

    async def serve(self, *, once=False):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: setattr(self, "stopping", True))
            except (NotImplementedError, RuntimeError):
                pass
        while not self.stopping:
            await self.recover()
            result = await self.once()
            if once:
                return result
            if result["state"] == "idle":
                await asyncio.sleep(5)
        await self.store.drain(self.worker_id)
        return {"worker_id": self.worker_id, "state": "drained"}
