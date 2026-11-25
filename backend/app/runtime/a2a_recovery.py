"""Durable remote observation cooldowns on the existing fault/circuit ledger."""

import random
from uuid import uuid4

from sqlalchemy import select

from . import faults
from .a2a_faults import code
from .database import RuntimeErrorCode
from .fault_schema import circuits, plans
from .remote_control import TransactionView
from .schema import attempts
from .store import TERMINAL, RuntimeStore


class A2ARecovery:
    def __init__(self, execution):
        self.execution, self.store, self.spec = (
            execution,
            execution.store,
            execution.spec,
        )
        self.policy = self.spec.recovery_policy()

    async def permit(self, run, attempt, *, reconcile):
        async with self.store.db.transaction(self.store.scope) as c:
            run, attempt, now = await self.execution.checked(
                c, run, attempt, reconcile=reconcile
            )
            poll = attempt["remote"].get("a2a_poll", {})
            if max(poll.get("retry_at", 0), poll.get("lease_until", 0)) > now:
                return None
            state = await faults.circuit(c, self.store.scope, self.policy["resource"])
            if state and state["state"] != "closed":
                if state["retry_at"] > now:
                    return None
                if state["state"] == "half_open":
                    probe = (
                        (
                            await c.execute(
                                select(attempts).where(
                                    attempts.c.id == state["probe_attempt"]
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if probe and probe["id"] != attempt["id"]:
                        lease = (
                            probe["remote"].get("a2a_poll", {}).get("lease_until", 0)
                        )
                        if lease > now or (
                            probe["state"]
                            not in {
                                "result_unknown",
                                "failed",
                                "completed",
                                "cancelled",
                            }
                            and probe["lease_until"] > now
                        ):
                            return None
                await c.execute(
                    circuits.update()
                    .where(
                        circuits.c.scope == self.store.scope,
                        circuits.c.resource == self.policy["resource"],
                    )
                    .values(
                        state="half_open", probe_attempt=attempt["id"], updated_at=now
                    )
                )
            token = str(uuid4())
            # Query, optional cancel and result download all have bounded I/O.
            poll = {
                **poll,
                "token": token,
                "lease_until": now + 3 * self.spec.request_timeout + 10,
            }
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(remote={**attempt["remote"], "a2a_poll": poll})
            )
            return token

    async def failure(self, run, attempt, exc, *, token=None, reconcile=False):
        async with self.store.db.transaction(self.store.scope) as c:
            # CAS cancellation races are resolved by re-reading this same Attempt;
            # ownership/epoch checks still apply to the original invocation.
            current = await self.store._run(c, run["id"])
            if current["state"] in TERMINAL:
                return
            current, a, now = await self.execution.checked(
                c, current, attempt, reconcile=reconcile
            )
            poll = a["remote"].get("a2a_poll", {})
            if token is not None and (
                poll.get("token") != token or poll.get("lease_until", 0) <= now
            ):
                raise RuntimeErrorCode("STALE_OWNER")
            failure = code(exc)
            count = poll.get("failures", 0) + 1
            prior = await faults.circuit(c, self.store.scope, self.policy["resource"])
            base = faults.delay(
                self.policy,
                max(count, (prior["failures"] + 1) if prior else 1),
                faults.category(failure),
            )
            # Equal jitter prevents synchronized retries, and never precedes the
            # explicitly bounded Retry-After received from the remote service.
            wait = max(base * random.uniform(0.5, 1), getattr(exc, "retry_after", 0))
            retry_at = now + wait
            await faults.outcome(c, self.store.scope, a["id"], False, now, failure)
            circuit = await faults.circuit(c, self.store.scope, self.policy["resource"])
            if circuit and circuit["state"] != "closed":
                retry_at = max(retry_at, prior["retry_at"] if prior else 0)
                await c.execute(
                    circuits.update()
                    .where(
                        circuits.c.scope == self.store.scope,
                        circuits.c.resource == self.policy["resource"],
                    )
                    .values(retry_at=retry_at)
                )
            poll = {
                "failures": count,
                "retry_at": retry_at,
                "lease_until": 0,
                "token": None,
            }
            await c.execute(
                attempts.update()
                .where(attempts.c.id == a["id"])
                .values(
                    remote={**a["remote"], "a2a_poll": poll},
                    error={
                        "code": failure,
                        "category": faults.category(failure),
                        "retry_at": retry_at,
                    },
                )
            )
            await faults.put_plan(
                c,
                {
                    "run_id": run["id"],
                    "scope": self.store.scope,
                    "attempt_id": a["id"],
                    "resource": self.policy["resource"],
                    "state": "observing",
                    "category": faults.category(failure),
                    "code": failure,
                    "retry_at": retry_at,
                    "reason": "QUERY_ORIGINAL_TASK",
                    "updated_at": now,
                },
            )
            if not reconcile:
                bound = RuntimeStore(
                    TransactionView(self.store.db, c, self.store.scope),
                    self.store.scope,
                )
                await bound.mark_unknown(
                    (
                        run["id"],
                        a["id"],
                        a["worker_id"],
                        a["worker_instance"],
                        a["epoch"],
                    ),
                    failure,
                )
            else:
                await self.store._event(
                    c,
                    current,
                    "a2a.observation_deferred",
                    {"code": failure, "retry_at": retry_at},
                    now,
                )

    async def success(self, run, attempt, token, *, reconcile):
        async with self.store.db.transaction(self.store.scope) as c:
            current = await self.store._run(c, run["id"])
            current, a, now = await self.execution.checked(
                c, current, attempt, reconcile=reconcile
            )
            poll = a["remote"].get("a2a_poll", {})
            if poll.get("token") != token or poll.get("lease_until", 0) <= now:
                raise RuntimeErrorCode("STALE_OWNER")
            await faults.outcome(c, self.store.scope, a["id"], True, now)
            await c.execute(
                attempts.update()
                .where(attempts.c.id == a["id"])
                .values(
                    remote={
                        **a["remote"],
                        "a2a_poll": {
                            "failures": 0,
                            "retry_at": 0,
                            "lease_until": 0,
                            "token": None,
                        },
                    },
                    error=None,
                )
            )
            await c.execute(
                plans.update()
                .where(plans.c.run_id == run["id"])
                .values(state="observed", retry_at=None, reason=None, updated_at=now)
            )
            if poll.get("failures"):
                await self.store._event(
                    c,
                    current,
                    "a2a.observation_recovered",
                    {"attempt_id": a["id"]},
                    now,
                )

    async def preflight_failed(self, run, attempt, exc):
        async with self.store.db.transaction(self.store.scope) as c:
            binding = (
                run["id"],
                attempt["id"],
                attempt["worker_id"],
                attempt["worker_instance"],
                attempt["epoch"],
            )
            current, a, now = await self.store._owned(c, *binding)
            bound = RuntimeStore(
                TransactionView(self.store.db, c, self.store.scope), self.store.scope
            )
            await bound.reject_unlaunched(
                binding,
                code(exc),
                retry_unlaunched=True,
                retry_after=getattr(exc, "retry_after", 0),
            )
            state = await faults.circuit(c, self.store.scope, self.policy["resource"])
            if state and state["state"] != "closed":
                await c.execute(
                    circuits.update()
                    .where(
                        circuits.c.scope == self.store.scope,
                        circuits.c.resource == self.policy["resource"],
                    )
                    .values(
                        retry_at=max(
                            state["retry_at"], now + getattr(exc, "retry_after", 0)
                        )
                    )
                )

    async def release(self, run, attempt, token, *, reconcile):
        async with self.store.db.transaction(self.store.scope) as c:
            current = await self.store._run(c, run["id"])
            if current["state"] in TERMINAL:
                return
            _, a, _ = await self.execution.checked(
                c, current, attempt, reconcile=reconcile
            )
            poll = a["remote"].get("a2a_poll", {})
            if poll.get("token") == token:
                await c.execute(
                    attempts.update()
                    .where(attempts.c.id == a["id"])
                    .values(
                        remote={
                            **a["remote"],
                            "a2a_poll": {**poll, "token": None, "lease_until": 0},
                        }
                    )
                )
