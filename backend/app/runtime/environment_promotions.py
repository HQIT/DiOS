"""Evaluation-backed environment admission at the actual Deployment boundary."""

import asyncio
from urllib.parse import urlsplit

from sqlalchemy import select

from . import technical_records as ledger
from .database import RuntimeErrorCode
from .deployment_schema import deployments
from .execution_revisions import load_profile
from .lifecycle import admission
from .object_lifetime import ObjectLifetime
from .remote_control import TransactionView
from .store import bounded, digest, identifier

STAGES = {"development": 0, "test": 1, "production": 2}


def permission(environment_id):
    return "environment." + environment_id + ".promote"


def external_reference(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"uri", "version", "digest"}:
        raise RuntimeErrorCode("INVALID_EXTERNAL_APPROVAL_REFERENCE")
    from .revisions import DIGEST, safe_configuration

    safe_configuration(value)
    uri = value["uri"]
    if (
        not isinstance(uri, str)
        or len(uri) > 2048
        or urlsplit(uri).scheme not in {"https", "urn"}
        or not isinstance(value["version"], str)
        or not 1 <= len(value["version"]) <= 160
        or not isinstance(value["digest"], str)
        or not DIGEST.fullmatch(value["digest"])
    ):
        raise RuntimeErrorCode("INVALID_EXTERNAL_APPROVAL_REFERENCE")
    return dict(value)


async def evaluation_gate(c, db, scope, document, policy):
    environment = await ledger.get(
        c, scope, "environment", document["environment_ref"]["id"]
    )
    if ledger.reference(environment) != document["environment_ref"]:
        raise RuntimeErrorCode("ENVIRONMENT_POLICY_CHANGED")
    rules = environment["document"]
    evaluation = await ledger.get(c, scope, "evaluation", document["evaluation_id"])
    report = evaluation["document"]
    if (
        report["state"] != "passed"
        or report["request"]["revision_ref"] != document["revision_ref"]
        or report["request"]["suite_ref"] != rules["suite_ref"]
    ):
        raise RuntimeErrorCode("EVALUATION_GATE_FAILED")
    if await db.now(c) - report["completed_at"] > rules["max_age_seconds"]:
        raise RuntimeErrorCode("EVALUATION_EXPIRED")
    if rules["external_approval_required"] and document["external_approval"] is None:
        raise RuntimeErrorCode("EXTERNAL_APPROVAL_REFERENCE_REQUIRED")
    raw, profile, spec, mode, refs = await load_profile(
        TransactionView(db, c, scope), scope, document["revision_ref"]
    )
    if mode != "e2ag" or policy is None:
        raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
    grant = await asyncio.to_thread(policy.admission, profile, spec.fingerprint(), refs)
    if grant["policy_digest"] != report["policy_digest"]:
        raise RuntimeErrorCode("EVALUATION_POLICY_CHANGED")
    for item in report["measurements"]:
        if item["state"] != "passed":
            raise RuntimeErrorCode("EVALUATION_GATE_FAILED")
        await ObjectLifetime(db, scope).available(c, item["artifact"])
        if raw["bridge"] == "a2a-0.3-jsonrpc":
            from .endpoint_compatibility import Compatibility

            if not item.get("endpoint_digest"):
                raise RuntimeErrorCode("COMPATIBILITY_REVIEW_REQUIRED")
            await Compatibility(db, scope).require_accepted(
                c, spec.fingerprint(), item["endpoint_digest"]
            )
    if rules["source_environment"] and not document["rollback"]:
        if not document["source_deployment"]:
            raise RuntimeErrorCode("PROMOTION_SOURCE_REQUIRED")
        source = await ledger.get(c, scope, "binding", document["source_deployment"])
        if (
            source["document"]["environment_id"] != rules["source_environment"]
            or source["document"]["definition_id"] != profile
        ):
            raise RuntimeErrorCode("PROMOTION_SOURCE_MISMATCH")
        row = (
            (
                await c.execute(
                    select(deployments).where(
                        deployments.c.scope == scope, deployments.c.id == source["id"]
                    )
                )
            )
            .mappings()
            .first()
        )
        if (
            not row
            or row["state"] != "ready"
            or row["active_revision_id"] != document["revision_ref"]
        ):
            raise RuntimeErrorCode("PROMOTION_SOURCE_NOT_READY")
    return grant


async def require_promotion(
    c, db, scope, deployment_id, revision_ref, key, probe, policy, approvers
):
    binding = await ledger.get(c, scope, "binding", deployment_id, optional=True)
    if binding is None:
        return
    item = await ledger.get(c, scope, "promotion", key, optional=True)
    if not item or item["document"]["state"] not in {"approved", "published"}:
        raise RuntimeErrorCode("ENVIRONMENT_PROMOTION_REQUIRED")
    document = item["document"]
    if (
        document["deployment_id"] != deployment_id
        or document["revision_ref"] != revision_ref
        or document["environment_ref"]["id"] != binding["document"]["environment_id"]
        or document["probe"] != probe
    ):
        raise RuntimeErrorCode("PROMOTION_BINDING_MISMATCH")
    if approvers is None:
        raise RuntimeErrorCode("APPROVER_PROVIDER_REQUIRED")
    await asyncio.to_thread(
        approvers.eligible,
        document["approved_by"],
        scope,
        permission(binding["document"]["environment_id"]),
    )
    await evaluation_gate(c, db, scope, document, policy)


class Promotions:
    def __init__(self, db, scope, policy, approvers):
        self.db, self.scope, self.policy, self.approvers = db, scope, policy, approvers

    async def environment(self, name, value, version, actor):
        identifier(name)
        bounded(value, 8192)
        fields = {
            "stage",
            "suite_ref",
            "max_age_seconds",
            "source_environment",
            "external_approval_required",
        }
        if (
            len(name) > 100
            or not isinstance(value, dict)
            or set(value) != fields
            or value["stage"] not in STAGES
            or type(value["max_age_seconds"]) is not int
            or not 60 <= value["max_age_seconds"] <= 2592000
            or type(value["external_approval_required"]) is not bool
        ):
            raise RuntimeErrorCode("INVALID_ENVIRONMENT_POLICY")
        async with self.db.transaction(self.scope) as c:
            if not await admission(c, self.scope):
                raise RuntimeErrorCode("RUNTIME_DRAINING")
            await ledger.resolve(c, self.scope, "suite", value["suite_ref"])
            old = await ledger.get(c, self.scope, "environment", name, optional=True)
            if old and any(
                old["document"][key] != value[key]
                for key in ("stage", "source_environment")
            ):
                raise RuntimeErrorCode("ENVIRONMENT_TOPOLOGY_IMMUTABLE")
            source = value["source_environment"]
            if source is not None:
                previous = await ledger.get(c, self.scope, "environment", source)
                if STAGES[previous["document"]["stage"]] >= STAGES[value["stage"]]:
                    raise RuntimeErrorCode("ENVIRONMENT_STAGE_ORDER_INVALID")
            elif value["stage"] == "production":
                raise RuntimeErrorCode("PROMOTION_SOURCE_REQUIRED")
            row = await ledger.append(
                c, self.db, self.scope, "environment", name, value, actor, version
            )
            return dict(row, ref=ledger.reference(row))

    async def bind(self, deployment_id, environment_id, definition_id, actor):
        identifier(definition_id)
        async with self.db.transaction(self.scope) as c:
            if not await admission(c, self.scope):
                raise RuntimeErrorCode("RUNTIME_DRAINING")
            await ledger.get(c, self.scope, "environment", environment_id)
            value = {"environment_id": environment_id, "definition_id": definition_id}
            prior = await ledger.get(
                c, self.scope, "binding", deployment_id, optional=True
            )
            if prior:
                if prior["document"] != value:
                    raise RuntimeErrorCode("ENVIRONMENT_BINDING_IMMUTABLE")
                return prior
            if (
                await c.execute(
                    select(deployments.c.id).where(
                        deployments.c.scope == self.scope,
                        deployments.c.id == deployment_id,
                    )
                )
            ).first():
                raise RuntimeErrorCode("ENVIRONMENT_BINDING_REQUIRES_NEW_DEPLOYMENT")
            return await ledger.append(
                c, self.db, self.scope, "binding", deployment_id, value, actor
            )

    async def request(
        self,
        deployment_id,
        evaluation_id,
        deployment_version,
        source_deployment,
        external_approval,
        key,
        actor,
        *,
        rollback=False,
    ):
        identifier(key)
        if (
            type(deployment_version) is not int
            or deployment_version < 0
            or type(rollback) is not bool
        ):
            raise RuntimeErrorCode("INVALID_PROMOTION_REQUEST")
        external_approval = external_reference(external_approval)
        identity = {
            "deployment_id": deployment_id,
            "evaluation_id": evaluation_id,
            "deployment_version": deployment_version,
            "source_deployment": source_deployment,
            "external_approval": external_approval,
            "rollback": rollback,
            "actor": actor,
        }
        promotion_id = "promotion-" + digest([self.scope, key])
        async with self.db.transaction(self.scope) as c:
            old = await ledger.get(
                c, self.scope, "promotion", promotion_id, optional=True
            )
            if old:
                if old["document"]["request_digest"] != digest(identity):
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return old
            if not await admission(c, self.scope):
                raise RuntimeErrorCode("RUNTIME_DRAINING")
            binding = await ledger.get(c, self.scope, "binding", deployment_id)
            environment = await ledger.get(
                c, self.scope, "environment", binding["document"]["environment_id"]
            )
            evaluation = await ledger.get(c, self.scope, "evaluation", evaluation_id)
            revision_ref = evaluation["document"]["request"]["revision_ref"]
            raw, profile, _, _, _ = await load_profile(
                TransactionView(self.db, c, self.scope), self.scope, revision_ref
            )
            if profile != binding["document"]["definition_id"]:
                raise RuntimeErrorCode("PROMOTION_DEFINITION_MISMATCH")
            if rollback:
                active = (
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
                if not active or active["previous_revision_id"] != revision_ref:
                    raise RuntimeErrorCode("DEPLOYMENT_ROLLBACK_UNAVAILABLE")
            spec = await ledger.resolve(
                c, self.scope, "suite", environment["document"]["suite_ref"]
            )
            from .technical_evaluations import case_input

            document = dict(
                identity,
                state="pending",
                request_digest=digest(identity),
                environment_ref=ledger.reference(environment),
                revision_ref=revision_ref,
                approved_by=None,
                probe=case_input(raw, spec["document"]["cases"][0]),
            )
            await evaluation_gate(c, self.db, self.scope, document, self.policy)
            return await ledger.append(
                c, self.db, self.scope, "promotion", promotion_id, document, actor
            )

    async def decide(self, promotion_id, version, decision, token, reason):
        if (
            decision not in {"approved", "denied"}
            or not isinstance(reason, str)
            or len(reason) > 2000
        ):
            raise RuntimeErrorCode("INVALID_APPROVAL_DECISION")
        if self.approvers is None:
            raise RuntimeErrorCode("APPROVER_PROVIDER_REQUIRED")
        async with self.db.transaction(self.scope) as c:
            current = await ledger.get(c, self.scope, "promotion", promotion_id)
            document = current["document"]
            operation = permission(document["environment_ref"]["id"])
            actor = await asyncio.to_thread(
                self.approvers.authenticate, token, self.scope, operation
            )
            if (
                current["version"] == version + 1
                and document["state"] == decision
                and current["actor"] == actor
                and document.get("reason") == reason
            ):
                return current
            if current["version"] != version:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            if document["state"] != "pending":
                raise RuntimeErrorCode("APPROVAL_ALREADY_DECIDED")
            if actor == document["actor"]:
                raise RuntimeErrorCode("APPROVER_MUST_BE_INDEPENDENT")
            if decision == "approved":
                if not await admission(c, self.scope):
                    raise RuntimeErrorCode("RUNTIME_DRAINING")
                grant = await evaluation_gate(
                    c, self.db, self.scope, document, self.policy
                )
                if actor in {grant["actor"], grant["subject"]}:
                    raise RuntimeErrorCode("APPROVER_MUST_BE_INDEPENDENT")
            if self.approvers.authenticate(token, self.scope, operation) != actor:
                raise RuntimeErrorCode("APPROVER_FORBIDDEN")
            return await ledger.append(
                c,
                self.db,
                self.scope,
                "promotion",
                promotion_id,
                dict(
                    document,
                    state=decision,
                    approved_by=actor if decision == "approved" else None,
                    reason=reason,
                ),
                actor,
                version,
            )

    async def publish(self, promotion_id, version, actor):
        failure = None
        async with self.db.transaction(self.scope) as c:
            current = await ledger.get(c, self.scope, "promotion", promotion_id)
            document = current["document"]
            if document["state"] == "published" and current["version"] == version + 1:
                return current
            if current["version"] != version:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            if document["state"] != "approved":
                raise RuntimeErrorCode("ENVIRONMENT_PROMOTION_REQUIRED")
            if self.approvers is None:
                raise RuntimeErrorCode("APPROVER_PROVIDER_REQUIRED")
            try:
                await asyncio.to_thread(
                    self.approvers.eligible,
                    document["approved_by"],
                    self.scope,
                    permission(document["environment_ref"]["id"]),
                )
            except RuntimeErrorCode as exc:
                if exc.code != "APPROVER_FORBIDDEN":
                    raise
                failure = exc
                await ledger.append(
                    c,
                    self.db,
                    self.scope,
                    "promotion",
                    promotion_id,
                    dict(document, state="invalidated", error=exc.code),
                    actor,
                    version,
                )
            if failure is None:
                from .deployments import Deployments

                manager = Deployments(
                    TransactionView(self.db, c, self.scope),
                    self.scope,
                    self.policy,
                    self.approvers,
                )
                result = await manager.request(
                    document["deployment_id"],
                    document["revision_ref"],
                    promotion_id,
                    document["deployment_version"],
                    actor,
                    document["probe"],
                    timeout=600,
                    rollback=document["rollback"],
                )
                return await ledger.append(
                    c,
                    self.db,
                    self.scope,
                    "promotion",
                    promotion_id,
                    dict(document, state="published", deployment=result),
                    actor,
                    version,
                )
        raise failure
