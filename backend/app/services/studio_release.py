"""Digest-bound release review composed with immutable Revisions and Deployments."""

import asyncio
import json

from sqlalchemy import select

from app.runtime.database import RuntimeErrorCode
from app.runtime.deployments import Deployments
from app.runtime.execution_revisions import compile_profile, decode
from app.runtime.lifecycle import admission
from app.runtime.remote_control import TransactionView
from app.runtime.revisions import Revisions, manifest
from app.runtime.store import RuntimeStore, bounded, digest, identifier
from app.runtime.studio_release_schema import releases, require
from app.services.configuration_studio import compare
from app.services.task_inputs import dynamic_input, registration

PERMISSION = "studio.publish"


def prepare(base, draft, sample_id=None, input_payload=None):
    review = compare(base, draft)
    if not review["valid"]:
        raise RuntimeErrorCode("INVALID_STUDIO_DRAFT")
    raw = json.loads(draft)
    # HTTP intake accepts frozen data. Resolving local paths/building images uses
    # the existing trusted CLI publisher; never read caller-selected host files.
    display = raw.get("console", {})
    if display.get("enabled") is not True or "input_catalog" in display:
        raise RuntimeErrorCode("STUDIO_INLINE_INPUT_CATALOG_REQUIRED")
    registered = registration(display)
    if registered["result_schema"] is None:
        raise RuntimeErrorCode("STUDIO_RESULT_SCHEMA_REQUIRED")
    if (sample_id is None) == (input_payload is None):
        raise RuntimeErrorCode("INVALID_WORK_INPUT")
    sample = (
        dynamic_input(registered, input_payload)
        if input_payload is not None
        else next((x for x in registered["inputs"] if x["id"] == sample_id), None)
    )
    if sample is None:
        raise RuntimeErrorCode("SAMPLE_NOT_FOUND")
    value = manifest(compile_profile(raw))
    if value["configuration"]["governance_mode"] != "e2ag":
        raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
    probe = {
        "payload": {"goal": "Validate this candidate configuration", "sample": sample},
        "input_registration": {
            "digest": registered["digest"],
            "result_schema": registered["result_schema"],
        },
        "task_kind": registered["kind"],
    }
    bounded(probe, 12000)
    return {
        "base": base,
        "draft": draft,
        "review": review,
        "manifest": value,
        "probe": probe,
    }


class StudioReleases:
    def __init__(self, console, approvers):
        self.db, self.scope, self.policy = console.db, console.scope, console.policy
        self.approvers = approvers

    async def _row(self, c, release_id):
        await require(c)
        value = (
            (
                await c.execute(
                    select(releases)
                    .where(releases.c.scope == self.scope, releases.c.id == release_id)
                    .order_by(releases.c.version.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if value is None:
            raise RuntimeErrorCode("NOT_FOUND")
        return dict(value)

    async def _append(self, c, current, state, actor, detail):
        value = dict(
            current,
            version=current["version"] + 1,
            state=state,
            actor=actor,
            detail=detail,
            created_at=await self.db.now(c),
        )
        await c.execute(releases.insert().values(**value))
        return value

    def _grant(self, snapshot):
        _raw, profile, spec, mode, refs = decode({"manifest": snapshot["manifest"]})
        if self.policy is None or mode != "e2ag":
            raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
        return self.policy.admission(profile, spec.fingerprint(), refs)

    def _current_grant(self, snapshot):
        grant = self._grant(snapshot)
        if grant["policy_digest"] != snapshot["policy_digest"]:
            raise RuntimeErrorCode("STUDIO_POLICY_CHANGED")
        return grant

    async def show(self, release_id):
        identifier(release_id)
        async with self.db.read(self.scope) as c:
            value = await self._row(c, release_id)
            value["expired"] = await self.db.now(c) >= value["snapshot"]["expires_at"]
        return value

    async def probe(self, release_id):
        request = await self.show(release_id)
        if request["state"] != "published":
            raise RuntimeErrorCode("STUDIO_PROBE_NOT_READY")
        store = RuntimeStore(self.db, self.scope)
        run = await store.get(request["detail"]["probe_run_id"])
        if run["input"].get("revision_ref") != request["detail"]["revision_ref"]:
            raise RuntimeErrorCode("STUDIO_PROBE_BINDING_INVALID")
        from app.runtime.work_authorizations import view

        fields = (
            "id",
            "state",
            "session_id",
            "worker_id",
            "process_id",
            "error",
            "started_at",
            "finished_at",
        )
        return {
            "run_id": run["id"],
            "state": run["state"],
            "revision_ref": run["input"]["revision_ref"],
            "attempts": [{key: row[key] for key in fields} for row in run["attempts"]],
            "authorization": await view(store, run["id"]),
            "observed_at": run["observed_at"],
        }

    async def list(self, after="", limit=30):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise RuntimeErrorCode("INVALID_LIMIT")
        async with self.db.read(self.scope) as c:
            await require(c)
            ids = (
                (
                    await c.execute(
                        select(releases.c.id)
                        .where(
                            releases.c.scope == self.scope,
                            releases.c.id > after,
                            releases.c.version == 1,
                        )
                        .order_by(releases.c.id)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {k: row[k] for k in ("id", "version", "state", "actor", "created_at")}
                | {
                    "deployment_id": row["snapshot"]["deployment_id"],
                    "draft_digest": row["snapshot"]["review"]["draft_digest"],
                }
                for release_id in ids
                for row in [await self._row(c, release_id)]
            ]

    async def request(
        self,
        base,
        draft,
        deployment_id,
        deployment_version,
        key,
        actor,
        sample_id=None,
        input_payload=None,
    ):
        for value in (deployment_id, key, actor):
            identifier(value)
        if type(deployment_version) is not int or deployment_version < 0:
            raise RuntimeErrorCode("INVALID_EXPECTED_VERSION")
        snapshot = await asyncio.to_thread(
            prepare, base, draft, sample_id, input_payload
        )
        snapshot.update(
            deployment_id=deployment_id,
            deployment_version=deployment_version,
            requested_by=actor,
        )
        request_digest = digest(snapshot)
        release_id = "release-" + digest([self.scope, key])
        async with self.db.transaction(self.scope) as c:
            await require(c)
            try:
                old = await self._row(c, release_id)
            except RuntimeErrorCode as exc:
                if exc.code != "NOT_FOUND":
                    raise
            else:
                if old["request_digest"] != request_digest:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return old
            if not await admission(c, self.scope):
                raise RuntimeErrorCode("RUNTIME_DRAINING")
            grant = await asyncio.to_thread(self._grant, snapshot)
            snapshot.update(
                policy_digest=grant["policy_digest"],
                expires_at=await self.db.now(c) + 86400,
            )
            current = {
                "scope": self.scope,
                "id": release_id,
                "version": 0,
                "request_digest": request_digest,
                "snapshot": snapshot,
            }
            return await self._append(c, current, "pending", actor, {})

    async def decide(self, release_id, version, decision, token, reason=""):
        if decision not in {"approved", "denied"} or len(reason) > 2000:
            raise RuntimeErrorCode("INVALID_APPROVAL_DECISION")
        if self.approvers is None:
            raise RuntimeErrorCode("APPROVER_PROVIDER_REQUIRED")
        async with self.db.transaction(self.scope) as c:
            current = await self._row(c, release_id)
            actor = await asyncio.to_thread(
                self.approvers.authenticate, token, self.scope, PERMISSION
            )
            detail = {"reason": reason, "expected_version": version}
            if (
                current["version"] == version + 1
                and current["state"] == decision
                and current["actor"] == actor
                and current["detail"] == detail
            ):
                return current
            if current["version"] != version:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            if current["state"] != "pending":
                raise RuntimeErrorCode("APPROVAL_ALREADY_DECIDED")
            snapshot = current["snapshot"]
            if await self.db.now(c) >= snapshot["expires_at"]:
                raise RuntimeErrorCode("APPROVAL_EXPIRED")
            if actor == snapshot["requested_by"]:
                raise RuntimeErrorCode("APPROVER_MUST_BE_INDEPENDENT")
            if decision == "approved":
                if not await admission(c, self.scope):
                    raise RuntimeErrorCode("RUNTIME_DRAINING")
                grant = await asyncio.to_thread(self._current_grant, snapshot)
                if actor in {grant["subject"], grant["actor"]}:
                    raise RuntimeErrorCode("APPROVER_MUST_BE_INDEPENDENT")
            if self.approvers.authenticate(token, self.scope, PERMISSION) != actor:
                raise RuntimeErrorCode("APPROVER_FORBIDDEN")
            return await self._append(c, current, decision, actor, detail)

    async def publish(self, release_id, version, actor):
        failure = None
        async with self.db.transaction(self.scope) as c:
            current = await self._row(c, release_id)
            if current["state"] == "published" and current["version"] == version + 1:
                return current
            if current["version"] != version:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            if current["state"] != "approved":
                raise RuntimeErrorCode("STUDIO_RELEASE_APPROVAL_REQUIRED")
            snapshot = current["snapshot"]
            if await self.db.now(c) >= snapshot["expires_at"]:
                raise RuntimeErrorCode("APPROVAL_EXPIRED")
            if self.approvers is None:
                raise RuntimeErrorCode("APPROVER_PROVIDER_REQUIRED")
            try:
                await asyncio.to_thread(
                    self.approvers.eligible, current["actor"], self.scope, PERMISSION
                )
            except RuntimeErrorCode as exc:
                if exc.code != "APPROVER_FORBIDDEN":
                    raise
                failure = exc
                await self._append(
                    c, current, "invalidated", actor, {"error": exc.code}
                )
            if failure is None:
                if not await admission(c, self.scope):
                    raise RuntimeErrorCode("RUNTIME_DRAINING")
                await asyncio.to_thread(self._current_grant, snapshot)
                bound = TransactionView(self.db, c, self.scope)
                revision = await Revisions(bound, self.scope).create(
                    release_id, snapshot["manifest"], actor=actor
                )
                deployment = await Deployments(bound, self.scope, self.policy).request(
                    snapshot["deployment_id"],
                    revision["revision"]["id"],
                    release_id,
                    snapshot["deployment_version"],
                    actor,
                    snapshot["probe"],
                    timeout=600,
                )
                # Keep publication/health admission and its receipt in one commit.
                self.approvers.eligible(current["actor"], self.scope, PERMISSION)
                await asyncio.to_thread(self._current_grant, snapshot)
                return await self._append(
                    c,
                    current,
                    "published",
                    actor,
                    {
                        "revision_ref": revision["revision"]["id"],
                        "deployment_id": snapshot["deployment_id"],
                        "probe_run_id": deployment["probe_run_id"],
                        "rollout_id": deployment["receipt"]["rollout_id"],
                    },
                )
        raise failure
