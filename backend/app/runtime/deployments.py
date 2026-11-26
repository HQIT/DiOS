"""Governed canary Run, natural drain and atomic version activation."""

import asyncio
from uuid import uuid4

from sqlalchemy import func, select

from .database import RuntimeErrorCode
from .deployment_schema import deployments, events, receipts, require, rollouts
from .execution_revisions import load_profile
from .lifecycle import admission
from .remote_control import TransactionView
from .schema import runs
from .store import TERMINAL, RuntimeStore, bounded, digest, identifier


async def row(c, scope, deployment_id):
    await require(c)
    value = (
        (
            await c.execute(
                select(deployments).where(
                    deployments.c.scope == scope, deployments.c.id == deployment_id
                )
            )
        )
        .mappings()
        .first()
    )
    if value is None:
        raise RuntimeErrorCode("NOT_FOUND")
    return dict(value)


async def admit_run(c, scope, deployment_id, revision_ref, key, rollout_id=None):
    selected = await row(c, scope, deployment_id)
    if rollout_id is not None:
        rollout = (
            (
                await c.execute(
                    select(rollouts).where(
                        rollouts.c.id == rollout_id,
                        rollouts.c.scope == scope,
                        rollouts.c.deployment_id == deployment_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if (
            not rollout
            or selected["rollout_id"] != rollout_id
            or rollout["state"] != "verifying"
            or rollout["revision_id"] != revision_ref
            or rollout["probe_key"] != key
        ):
            raise RuntimeErrorCode("DEPLOYMENT_PROBE_BINDING_INVALID")
    elif (
        selected["state"] not in {"ready", "pending"}
        or selected["active_revision_id"] != revision_ref
    ):
        raise RuntimeErrorCode("DEPLOYMENT_NOT_READY")


def representation(value):
    return {
        "deployment": {k: value[k] for k in ("id", "version", "revision_id", "state")}
        | {"resource_scope_ref": value["scope"]},
        "status": {
            k: value[k]
            for k in (
                "definition_id",
                "active_revision_id",
                "previous_revision_id",
                "rollout_id",
                "error",
                "created_at",
                "updated_at",
            )
        },
    }


class Deployments:
    def __init__(self, db, scope, policy=None):
        self.db, self.scope, self.policy = db, scope, policy
        self.store = RuntimeStore(db, scope)

    async def _event(self, c, current, kind, actor, now, detail, **changes):
        value = dict(current, **changes, version=current["version"] + 1, updated_at=now)
        await c.execute(
            deployments.update()
            .where(deployments.c.scope == self.scope, deployments.c.id == current["id"])
            .values(**value)
        )
        await c.execute(
            events.insert().values(
                scope=self.scope,
                deployment_id=value["id"],
                version=value["version"],
                rollout_id=value["rollout_id"],
                type=kind,
                actor=actor,
                detail=detail,
                created_at=now,
            )
        )
        return value

    async def _authorize(self, database, revision_ref):
        raw, profile, spec, mode, refs = await load_profile(
            database, self.scope, revision_ref
        )
        if "package_lock" in raw:
            from .package_admission import require_lock

            async with database.read(self.scope) as c:
                await require_lock(c, self.scope, raw["package_lock"])
        if mode != "e2ag" or self.policy is None:
            raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
        grant = await asyncio.to_thread(
            self.policy.admission, profile, spec.fingerprint(), refs
        )
        return profile, spec, refs, grant

    async def request(
        self,
        deployment_id,
        revision_ref,
        key,
        expected_version,
        actor,
        probe_input,
        *,
        timeout=300,
        rollback=False,
    ):
        for value in (deployment_id, key, actor):
            identifier(value)
        if (
            type(expected_version) is not int
            or expected_version < 0
            or type(timeout) is not int
            or not 10 <= timeout <= 3600
        ):
            raise RuntimeErrorCode("INVALID_DEPLOYMENT_REQUEST")
        if (
            not isinstance(probe_input, dict)
            or set(probe_input) - {"payload", "input_registration", "task_kind"}
            or not isinstance(probe_input.get("payload"), dict)
        ):
            raise RuntimeErrorCode("INVALID_DEPLOYMENT_PROBE")
        bounded(probe_input, 12000)
        request_hash = digest(
            [
                deployment_id,
                revision_ref,
                expected_version,
                actor,
                probe_input,
                timeout,
                rollback,
            ]
        )
        async with self.db.transaction(self.scope) as c:
            await require(c)
            prior = (
                (
                    await c.execute(
                        select(receipts).where(
                            receipts.c.scope == self.scope, receipts.c.key == key
                        )
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                if prior["request_digest"] != request_hash:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return dict(
                    representation(await row(c, self.scope, prior["deployment_id"])),
                    receipt=dict(prior),
                )
            if not await admission(c, self.scope):
                raise RuntimeErrorCode("RUNTIME_DRAINING")
            current = (
                (
                    await c.execute(
                        select(deployments).where(
                            deployments.c.scope == self.scope,
                            deployments.c.id == deployment_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (current["version"] if current else 0) != expected_version:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            if current and current["state"] in {"pending", "draining"}:
                raise RuntimeErrorCode("DEPLOYMENT_ROLLOUT_ACTIVE")
            # A failed observation deadline leaves its original probe intact.
            # Do not create replacement work while that execution is unresolved.
            unsettled = (
                await c.execute(
                    select(func.count())
                    .select_from(
                        rollouts.join(runs, rollouts.c.probe_run_id == runs.c.id)
                    )
                    .where(
                        rollouts.c.scope == self.scope,
                        rollouts.c.deployment_id == deployment_id,
                        runs.c.state.not_in(TERMINAL),
                    )
                )
            ).scalar_one()
            if unsettled:
                raise RuntimeErrorCode("DEPLOYMENT_PROBE_UNSETTLED")
            if rollback:
                if not current or not current["previous_revision_id"]:
                    raise RuntimeErrorCode("DEPLOYMENT_ROLLBACK_UNAVAILABLE")
                revision_ref = current["previous_revision_id"]
            bound_db = TransactionView(self.db, c, self.scope)
            profile, spec, refs, grant = await self._authorize(bound_db, revision_ref)
            if current and current["definition_id"] != profile:
                raise RuntimeErrorCode("DEPLOYMENT_DEFINITION_CHANGED")
            if current and current["active_revision_id"] == revision_ref:
                raise RuntimeErrorCode("DEPLOYMENT_ALREADY_ACTIVE")
            now = await self.db.now(c)
            rid = str(uuid4())
            probe_key = "deployment-probe:" + rid
            previous = current["active_revision_id"] if current else None
            rollout = {
                "id": rid,
                "scope": self.scope,
                "deployment_id": deployment_id,
                "revision_id": revision_ref,
                "previous_revision_id": previous,
                "state": "verifying",
                "probe_run_id": None,
                "probe_key": probe_key,
                "actor": actor,
                "operation": "rollback" if rollback else "deploy",
                "deadline": now + timeout,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            await c.execute(rollouts.insert().values(**rollout))
            if current is None:
                current = {
                    "scope": self.scope,
                    "id": deployment_id,
                    "definition_id": profile,
                    "version": 0,
                    "revision_id": revision_ref,
                    "active_revision_id": None,
                    "previous_revision_id": None,
                    "state": "pending",
                    "rollout_id": rid,
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                }
                await c.execute(deployments.insert().values(**current))
            current = await self._event(
                c,
                current,
                "deployment.requested",
                actor,
                now,
                {
                    "operation": rollout["operation"],
                    "revision_id": revision_ref,
                    "previous_revision_id": previous,
                },
                revision_id=revision_ref,
                rollout_id=rid,
                state="pending",
                error=None,
            )
            probe = await RuntimeStore(bound_db, self.scope).submit(
                probe_key,
                dict(
                    probe_input,
                    runtime_profile=profile,
                    profile_digest=spec.fingerprint(),
                    secret_refs=refs,
                    origin="deployment-health/v1",
                ),
                authorization=grant,
                revision_ref=revision_ref,
                deployment_id=deployment_id,
                deployment_rollout_id=rid,
            )
            await c.execute(
                rollouts.update()
                .where(rollouts.c.id == rid)
                .values(probe_run_id=probe["id"])
            )
            receipt = {
                "scope": self.scope,
                "key": key,
                "request_digest": request_hash,
                "deployment_id": deployment_id,
                "rollout_id": rid,
            }
            await c.execute(receipts.insert().values(**receipt))
            return dict(
                representation(current), receipt=receipt, probe_run_id=probe["id"]
            )

    async def reconcile(self, deployment_id):
        async with self.db.transaction(self.scope) as c:
            current = await row(c, self.scope, deployment_id)
            if current["state"] not in {"pending", "draining"}:
                return representation(current)
            rollout = dict(
                (
                    await c.execute(
                        select(rollouts).where(rollouts.c.id == current["rollout_id"])
                    )
                )
                .mappings()
                .one()
            )
            probe = await self.store._run(c, rollout["probe_run_id"])
            now = await self.db.now(c)
            failure = None
            if probe["state"] in {"failed", "cancelled"}:
                failure = "DEPLOYMENT_HEALTH_FAILED"
            elif probe["state"] != "succeeded":
                if now >= rollout["deadline"]:
                    failure = "DEPLOYMENT_HEALTH_DEADLINE"
                else:
                    return representation(current)
            if not failure:
                try:
                    _, _, _, grant = await self._authorize(
                        TransactionView(self.db, c, self.scope), rollout["revision_id"]
                    )
                    from .package_admission import check_work

                    await check_work(c, probe)
                    from .work_policy import policy_matches

                    if not policy_matches(grant, probe["input"]["work_policy_digest"]):
                        raise RuntimeErrorCode("WORK_POLICY_CHANGED")
                    from .a2a_profile import A2AProfile
                    from .endpoint_compatibility import Compatibility
                    from .schema import attempts

                    _, _, spec, _, _ = await load_profile(
                        TransactionView(self.db, c, self.scope),
                        self.scope,
                        rollout["revision_id"],
                    )
                    if isinstance(spec, A2AProfile):
                        attempt = (
                            (
                                await c.execute(
                                    select(attempts).where(
                                        attempts.c.id == probe["attempt_id"]
                                    )
                                )
                            )
                            .mappings()
                            .one()
                        )
                        declared = (
                            attempt["remote"].get("a2a", {}).get("declaration_digest")
                        )
                        if not declared:
                            raise RuntimeErrorCode("COMPATIBILITY_REVIEW_REQUIRED")
                        await Compatibility(self.db, self.scope).require_accepted(
                            c, spec.fingerprint(), declared, fresh=False
                        )
                except RuntimeErrorCode as exc:
                    failure = exc.code
            if failure:
                await c.execute(
                    rollouts.update()
                    .where(rollouts.c.id == rollout["id"])
                    .values(state="failed", error=failure, updated_at=now)
                )
                current = await self._event(
                    c,
                    current,
                    "deployment.rejected",
                    "deployment-controller",
                    now,
                    {
                        "probe_run_id": probe["id"],
                        "error": failure,
                        "retained_revision_id": current["active_revision_id"],
                    },
                    state="ready" if current["active_revision_id"] else "failed",
                    error=failure,
                )
                return representation(current)
            if current["state"] == "pending":
                current = await self._event(
                    c,
                    current,
                    "deployment.draining",
                    "deployment-controller",
                    now,
                    {"probe_run_id": probe["id"]},
                    state="draining",
                )
                await c.execute(
                    rollouts.update()
                    .where(rollouts.c.id == rollout["id"])
                    .values(state="draining", updated_at=now)
                )
            pending = (
                await c.execute(
                    select(func.count())
                    .select_from(runs)
                    .where(
                        runs.c.scope == self.scope,
                        runs.c.input["deployment_id"].as_string() == deployment_id,
                        runs.c.input["deployment_rollout_id"].as_string().is_(None),
                        runs.c.state.not_in(TERMINAL),
                    )
                )
            ).scalar_one()
            from .tool_schema import invocations

            tools = (
                await c.execute(
                    select(func.count())
                    .select_from(
                        invocations.join(runs, invocations.c.run_id == runs.c.id)
                    )
                    .where(
                        runs.c.scope == self.scope,
                        runs.c.input["deployment_id"].as_string() == deployment_id,
                        invocations.c.state.in_(["dispatched", "result_unknown"]),
                    )
                )
            ).scalar_one()
            if pending or tools:
                return dict(
                    representation(current),
                    unsettled_runs=pending,
                    unsettled_tool_calls=tools,
                )
            if not await admission(c, self.scope):
                return representation(current)
            await c.execute(
                rollouts.update()
                .where(rollouts.c.id == rollout["id"])
                .values(state="succeeded", updated_at=now)
            )
            current = await self._event(
                c,
                current,
                "deployment.activated",
                "deployment-controller",
                now,
                {
                    "probe_run_id": probe["id"],
                    "revision_id": rollout["revision_id"],
                    "previous_revision_id": current["active_revision_id"],
                },
                state="ready",
                previous_revision_id=current["active_revision_id"],
                active_revision_id=rollout["revision_id"],
                error=None,
            )
            return representation(current)

    async def show(self, deployment_id):
        async with self.db.read(self.scope) as c:
            current = await row(c, self.scope, deployment_id)
            rollout = (
                (
                    await c.execute(
                        select(rollouts).where(rollouts.c.id == current["rollout_id"])
                    )
                )
                .mappings()
                .one()
            )
            probe = await self.store._run(c, rollout["probe_run_id"])
            history = (
                (
                    await c.execute(
                        select(events)
                        .where(
                            events.c.scope == self.scope,
                            events.c.deployment_id == deployment_id,
                        )
                        .order_by(events.c.version.desc())
                        .limit(30)
                    )
                )
                .mappings()
                .all()
            )
            return dict(
                representation(current),
                rollout=dict(rollout),
                probe={
                    k: probe[k] for k in ("id", "state", "attempt_id", "updated_at")
                },
                events=[dict(e) for e in history],
                observed_at=await self.db.now(c),
            )

    async def list(self, *, after="", limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RuntimeErrorCode("INVALID_LIMIT")
        async with self.db.read(self.scope) as c:
            await require(c)
            rows = (
                (
                    await c.execute(
                        select(deployments)
                        .where(
                            deployments.c.scope == self.scope, deployments.c.id > after
                        )
                        .order_by(deployments.c.id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return [representation(value) for value in rows]
