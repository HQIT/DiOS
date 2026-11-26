"""Permission-filtered, paginated provenance over existing durable records.

No model text is interpreted as a relation and no new execution authority is
issued. Historical Revisions are read without loading today's runtime adapter.
"""

import asyncio

from sqlalchemy import and_, exists, or_, select

from app.runtime.database import RuntimeErrorCode
from app.runtime.history import page, page_args
from app.runtime.object_schema import links
from app.runtime.objects import reference
from app.runtime.revision_schema import revisions
from app.runtime.revisions import representation
from app.runtime.schema import artifacts, attempts
from app.runtime.tool_schema import invocations
from app.runtime.usage_schema import observations
from app.services.object_access import ObjectReader

SECTIONS = ("attempts", "tools", "artifacts", "sources", "usage")


class RunEvidence:
    def __init__(self, console):
        self.console = console
        self.reader = ObjectReader(console)

    async def allowed(self, run, action):
        try:
            await asyncio.to_thread(self.console.check_access, run, action)
            return True
        except RuntimeErrorCode as exc:
            if exc.code != "NOT_FOUND":
                raise
            return False

    async def object(self, ref):
        try:
            return await self.reader.get(ref)
        except RuntimeErrorCode as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None

    async def version(self, c, run):
        payload = run["input"]
        result = {
            "profile_id": payload.get("runtime_profile"),
            "profile_digest": payload.get("profile_digest"),
            "revision_ref": payload.get("revision_ref"),
            "deployment_id": payload.get("deployment_id"),
            "status": "not_recorded",
            "dependencies": {},
        }
        if not result["revision_ref"]:
            return result
        row = (
            (
                await c.execute(
                    select(revisions).where(
                        revisions.c.scope == self.console.scope,
                        revisions.c.id == result["revision_ref"],
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return dict(result, status="unavailable")
        saved = representation(row)
        result.update(
            status="recorded",
            digest=saved["revision"]["content_digest"],
            sequence=saved["sequence"],
            evidence_level="platform_recorded",
            dependencies={
                kind: [
                    {
                        key: item[key]
                        for key in ("name", "version", "digest", "visibility")
                    }
                    for item in items
                ]
                for kind, items in saved["dependencies"].items()
            },
        )
        return result

    def query(self, run_id, section):
        scope = self.console.scope
        if section == "attempts":
            return (
                select(attempts).where(attempts.c.run_id == run_id),
                (attempts.c.epoch,),
                [int],
            )
        if section == "artifacts":
            return (
                select(artifacts).where(artifacts.c.run_id == run_id),
                (artifacts.c.id,),
                [str],
            )
        if section == "tools":
            return (
                select(invocations).where(
                    invocations.c.scope == scope, invocations.c.run_id == run_id
                ),
                (invocations.c.id,),
                [str],
            )
        if section == "usage":
            return (
                select(observations).where(
                    observations.c.scope == scope, observations.c.run_id == run_id
                ),
                (observations.c.attempt_id, observations.c.sequence),
                [str, int],
            )
        attached_artifact = exists(
            select(artifacts.c.id).where(
                artifacts.c.run_id == run_id, artifacts.c.id == links.c.source_id
            )
        )
        attached_session = exists(
            select(attempts.c.id).where(
                attempts.c.run_id == run_id, attempts.c.id == links.c.source_id
            )
        )
        query = select(links).where(
            links.c.scope == scope,
            links.c.source_version == 1,
            or_(
                and_(links.c.source_kind == "artifact", attached_artifact),
                and_(links.c.source_kind == "session", attached_session),
            ),
        )
        return query, (links.c.id,), [str]

    async def project(self, section, row):
        if section == "attempts":
            value = {
                key: row[key]
                for key in (
                    "id",
                    "epoch",
                    "state",
                    "worker_id",
                    "worker_instance",
                    "started_at",
                    "finished_at",
                )
            }
            visible = (
                await self.object(reference("session", row["id"]))
                if row["session_id"]
                else None
            )
            value["session"] = visible["binding"] if visible else None
            native, remote = (
                row["remote"].get("runtime", {}),
                row["remote"].get("a2a", {}),
            )
            value["observation"] = {
                key: native.get(key) for key in ("adapter", "state", "observed_at")
            }
            value["evidence_level"] = "worker_observed" if native else "unknown"
            # Remote session/task identifiers share the session's object permission.
            if visible and remote:
                value["remote_task"] = {
                    key: remote.get(key)
                    for key in (
                        "task_id",
                        "context_id",
                        "stage",
                        "observed_state",
                        "observed_at",
                    )
                }
                value["evidence_level"] = "remote_reported"
            value["reason"] = (
                row["error"].get("code") if isinstance(row["error"], dict) else None
            )
            return value
        if section == "artifacts":
            ref = reference("artifact", row["id"])
            value = await self.object(ref)
            if value is None:
                return None
            return dict(
                id=row["id"],
                attempt_id=row["attempt_id"],
                object_ref=ref,
                descriptor=value["descriptor"],
                created_at=row["created_at"],
                evidence_level="platform_recorded",
            )
        if section == "sources":
            source = reference(
                row["source_kind"], row["source_id"], row["source_version"]
            )
            if await self.object(source) is None:
                return None
            target = row["target"]
            descriptor = None
            if "kind" in target:
                value = await self.object(target)
                if value is None:
                    return None
                descriptor = value["descriptor"]
            return dict(
                id=row["id"],
                source=source,
                relation=row["relation"],
                target=target,
                descriptor=descriptor,
                created_at=row["created_at"],
                evidence_level="operator_declared",
                verification="relation_recorded_content_use_not_inferred",
            )
        if section == "tools":
            result = {
                key: row[key]
                for key in (
                    "id",
                    "attempt_id",
                    "tool_id",
                    "registration_digest",
                    "arguments_digest",
                    "state",
                    "version",
                    "approval_state",
                    "created_at",
                    "updated_at",
                    "reason",
                )
            }
            result["permission"] = {
                key: row["permission"].get(key)
                for key in (
                    "action",
                    "resource_kind",
                    "resource_id",
                    "resource_version",
                    "constraint_revision",
                )
            }
            result["evidence_level"] = "platform_recorded"
            result["receipt_recorded"] = row["result_key"] is not None
            result["result_recorded"] = row["result_key"] is not None and row[
                "state"
            ] in {"succeeded", "failed"}
            result["result_evidence"] = (
                "remote_reported" if result["result_recorded"] else "unknown"
            )
            return result
        frame = row["value"]
        return dict(
            attempt_id=row["attempt_id"],
            sequence=row["sequence"],
            kind=row["kind"],
            quality=frame["quality"],
            values=frame["values"],
            recorded_at=row["recorded_at"],
            evidence_level="agent_reported"
            if frame["quality"] == "reported"
            else "unknown",
            aggregation="not_additive",
        )

    async def view(self, run_id, *, section="attempts", limit=50, cursor=None):
        from sqlalchemy import tuple_

        if section not in SECTIONS:
            raise RuntimeErrorCode("INVALID_EVIDENCE_SECTION")
        s = self.console
        run = await s.authorize(run_id, "run.read")
        binding = ["run-evidence-v1", s.scope, run_id, section, s.principal]
        query, order, types = self.query(run_id, section)
        after = page_args(limit, cursor, binding, types)
        if after:
            query = query.where(tuple_(*order) > tuple(after))
        permitted = section != "tools" or await self.allowed(run, "tool.read")
        if section in {"artifacts", "sources"}:
            permitted = permitted and await self.allowed(run, "artifact.read")
        async with s.db.read(s.scope) as c:
            root = dict(
                id=run_id,
                state=run["state"],
                version=run["version"],
                work_authorization_ref=run["input"].get("work_authorization_ref"),
                execution=await self.version(c, run),
            )
            now = await s.db.now(c)
            rows = (
                [
                    dict(x)
                    for x in (
                        await c.execute(query.order_by(*order).limit(limit + 1))
                    ).mappings()
                ]
                if permitted
                else []
            )
        raw = page(rows, limit, binding, lambda row: [row[col.name] for col in order])
        visible = []
        for row in raw["items"]:
            item = await self.project(section, row)
            if item is not None:
                visible.append(item)
        refresh = getattr(s, "credential_recheck", None)
        if refresh:
            await refresh()
        await s.authorize(run_id, "run.read")
        if permitted and section == "tools":
            await s.authorize(run_id, "tool.read")
        if permitted and section in {"artifacts", "sources"}:
            await s.authorize(run_id, "artifact.read")
        return dict(
            schema_version=1,
            root=root,
            section=section,
            observed_at=now,
            items=visible,
            next_cursor=raw["next_cursor"],
            access="granted" if permitted else "restricted",
            coverage=dict(
                scope="persisted_platform_relations",
                agent_internal="not_observed",
                page="more" if raw["next_cursor"] else "end",
                records="present" if visible else "none_visible",
                source_usage="explicit_relations_only",
                estimates="not_synthesized",
            ),
        )
