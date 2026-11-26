"""Bounded support bundles: allowlisted projections, scoped reads and integrity manifest."""

import hashlib
import hmac
import io
import json
import math
import secrets
import zipfile

from sqlalchemy import and_, func, or_, select

from .database import RuntimeErrorCode
from .fault_schema import plans
from .faults import CATEGORIES, category
from .schema import artifacts, attempts, events, projections
from .monitor_schema import loops
from .monitor import SERVICES, classify
from .tool_schema import evidence, invocations
from .work_auth_schema import decisions, grants, work

MAX_BYTES = 512 * 1024
LIMITS = {
    "attempts": 20,
    "events": 100,
    "work": 1,
    "grants": 20,
    "work_decisions": 100,
    "tools": 50,
    "tool_decisions": 100,
    "artifacts": 100,
    "recovery": 1,
    "schedulers": 20,
    "sync": 20,
}
STATES = set(
    "queued claimed preparing creating_session waiting_model streaming validating succeeded failed cancelled reconciliation_required retry_wait result_unknown running idle active revoked expired suspended granted pending approved denied dispatching completed unknown awaiting_approval prepared consumed not_required invalidated paused waiting observing observed exhausted abandoned closed open half_open enabled disabled".split()
)
ACTIONS = {
    "execute",
    "renew",
    "reconcile",
    "recover",
    "dispatch",
    "prepare",
    "approve",
    "approval",
    "policy",
    "result",
    "requested",
    "created",
    "preflight",
    "revoke",
    "cancel",
    "check",
}


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class Redactor:
    def __init__(self):
        self.salt = secrets.token_bytes(32)

    def ref(self, kind, value):
        if value is None:
            return None
        return (
            kind
            + "-"
            + hmac.new(
                self.salt, str(value).encode("utf-8"), hashlib.sha256
            ).hexdigest()[:24]
        )

    def code(self, value):
        if value is None:
            return None
        return value if value in CATEGORIES else self.ref("code", value)


def state(value):
    return value if isinstance(value, str) and value in STATES else "unknown"


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


async def build(
    c,
    scope,
    run,
    now,
    *,
    include_tools,
    include_artifacts,
    include_operational=True,
    object_allowed=None,
):
    redact = Redactor()
    coverage = {}

    async def rows(name, query, enabled=True):
        if not enabled:
            coverage[name] = {
                "included": False,
                "reason": "permission",
                "limit": LIMITS[name],
                "has_more": None,
            }
            return []
        records = (await c.execute(query.limit(LIMITS[name] + 1))).mappings().all()
        coverage[name] = {
            "included": True,
            "limit": LIMITS[name],
            "has_more": len(records) > LIMITS[name],
            "size_trimmed": False,
        }
        return records[: LIMITS[name]]

    async def allowed(kind, identity):
        return object_allowed is None or await object_allowed(kind, identity)

    data = {
        "run": {
            k: run[k]
            for k in (
                "id",
                "version",
                "epoch",
                "authorization_epoch",
                "revoked",
                "cancel_requested",
                "created_at",
                "updated_at",
            )
        }
    }
    data["run"].update(
        state=state(run["state"]), attempt=redact.ref("attempt", run["attempt_id"])
    )
    records = await rows(
        "attempts",
        select(
            *(
                attempts.c[k]
                for k in (
                    "id",
                    "epoch",
                    "state",
                    "session_id",
                    "worker_id",
                    "worker_instance",
                    "lease_until",
                    "authorization_epoch",
                    "started_at",
                    "finished_at",
                    "last_output_at",
                )
            ),
            attempts.c.error["code"].as_string().label("code"),
            attempts.c.remote["runtime"]["observed_at"].as_float().label("observed_at"),
            attempts.c.remote["runtime"]["alive"].as_boolean().label("alive"),
            attempts.c.remote["runtime"]["pid"].as_integer().label("pid"),
            attempts.c.remote["runtime"]["process_instance"]
            .as_string()
            .label("process_instance"),
            attempts.c.remote["runtime"]["state"].as_string().label("runtime_state"),
            attempts.c.remote["a2a"]["observed_at"]
            .as_float()
            .label("remote_observed_at"),
        )
        .where(attempts.c.run_id == run["id"])
        .order_by(attempts.c.epoch.desc()),
    )
    data["attempts"] = [
        {
            "attempt": redact.ref("attempt", r["id"]),
            "session": redact.ref("session", r["session_id"])
            if r["session_id"]
            and await allowed("session", r.get("attempt_id", r.get("id")))
            else None,
            "worker": redact.ref("worker", r["worker_id"]),
            "worker_instance": redact.ref("worker_instance", r["worker_instance"]),
            "state": state(r["state"]),
            "error_code": redact.code(r["code"]),
            "error_category": category(r["code"]) if r["code"] else None,
            "evidence_level": "remote_reported"
            if r["remote_observed_at"] is not None
            else "worker_observed"
            if r["observed_at"] is not None
            else "unknown",
            "observed_at": number(
                r["remote_observed_at"]
                if r["remote_observed_at"] is not None
                else r["observed_at"]
            ),
            "confirmed_running": bool(
                r["finished_at"] is None
                and r["state"] != "result_unknown"
                and r["lease_until"] > now
                and r["observed_at"] is not None
                and now - 15 <= r["observed_at"] <= now + 1
                and r["alive"]
                and r["pid"]
                and r["process_instance"]
                and r["runtime_state"] != "paused"
                and r["session_id"]
                and await allowed("session", r["id"])
            ),
            "seconds_since_output": max(0, now - r["last_output_at"])
            if r["last_output_at"] is not None
            else None,
            "open_seconds": max(0, now - r["started_at"])
            if r["finished_at"] is None
            else None,
            **{
                k: number(r[k])
                for k in (
                    "epoch",
                    "authorization_epoch",
                    "lease_until",
                    "started_at",
                    "finished_at",
                    "last_output_at",
                )
            },
        }
        for r in records
    ]
    records = await rows(
        "events",
        select(
            events.c.id,
            events.c.version,
            events.c.type,
            events.c.created_at,
            events.c.payload["state"].as_string().label("state"),
            events.c.payload["detail"]["attempt_id"].as_string().label("attempt_id"),
            events.c.payload["detail"]["phase"].as_string().label("phase"),
        )
        .where(events.c.run_id == run["id"])
        .order_by(events.c.version.desc()),
    )
    # Event types are runtime vocabulary; unknown extensions are pseudonymized.
    kinds = {
        "run.submitted",
        "run.completed",
        "run.cancelled",
        "run.revoked",
        "attempt.claimed",
        "attempt.phase",
        "attempt.failed",
        "attempt.output_retained",
        "attempt.exit_reconciled",
        "attempt.lease_expired",
        "run.retry_scheduled",
        "run.requeued",
    }
    data["events"] = [
        {
            "event": redact.ref("event", r["id"]),
            "version": r["version"],
            "type": r["type"]
            if r["type"] in kinds
            else redact.ref("event_type", r["type"]),
            "state": state(r["state"]),
            "phase": state(r["phase"]),
            "attempt": redact.ref("attempt", r["attempt_id"]),
            "created_at": number(r["created_at"]),
        }
        for r in records
    ]
    records = await rows(
        "work",
        select(
            *(
                work.c[k]
                for k in (
                    "id",
                    "state",
                    "version",
                    "authorization_epoch",
                    "created_at",
                    "expires_at",
                )
            )
        ).where(work.c.scope == scope, work.c.run_id == run["id"]),
    )
    data["work"] = [
        {
            "work": redact.ref("work", r["id"]),
            "state": state(r["state"]),
            **{
                k: number(r[k])
                for k in ("version", "authorization_epoch", "created_at", "expires_at")
            },
        }
        for r in records
    ]
    records = await rows(
        "grants",
        select(
            *(
                grants.c[k]
                for k in (
                    "attempt_id",
                    "authorization_id",
                    "session_id",
                    "state",
                    "lease_epoch",
                    "authorization_epoch",
                    "version",
                    "expires_at",
                )
            )
        )
        .where(grants.c.run_id == run["id"])
        .order_by(grants.c.lease_epoch.desc()),
    )
    data["grants"] = [
        {
            "attempt": redact.ref("attempt", r["attempt_id"]),
            "work": redact.ref("work", r["authorization_id"]),
            "session": redact.ref("session", r["session_id"])
            if r["session_id"]
            and await allowed("session", r.get("attempt_id", r.get("id")))
            else None,
            "state": state(r["state"]),
            **{
                k: number(r[k])
                for k in ("lease_epoch", "authorization_epoch", "version", "expires_at")
            },
        }
        for r in records
    ]

    def decision_columns(table):
        return [
            table.c.id,
            table.c.created_at,
            table.c.decision["decision"].as_string().label("outcome"),
        ]

    def decision_row(r):
        return {
            "audit": redact.ref("audit", r["id"]),
            "attempt": redact.ref("attempt", r["attempt_id"]),
            "action": r["action"]
            if r["action"] in ACTIONS
            else redact.ref("action", r["action"]),
            "outcome": r["outcome"]
            if r["outcome"] in {"allow", "deny", "error", "unknown"}
            else "unknown",
            "created_at": number(r["created_at"]),
        }

    records = await rows(
        "work_decisions",
        select(*decision_columns(decisions), decisions.c.attempt_id, decisions.c.action)
        .where(decisions.c.scope == scope, decisions.c.run_id == run["id"])
        .order_by(decisions.c.created_at.desc(), decisions.c.id),
    )
    data["work_decisions"] = [decision_row(r) for r in records]
    records = await rows(
        "tools",
        select(
            *(
                invocations.c[k]
                for k in (
                    "id",
                    "attempt_id",
                    "tool_id",
                    "state",
                    "version",
                    "approval_state",
                    "expires_at",
                    "created_at",
                    "updated_at",
                )
            )
        )
        .where(invocations.c.scope == scope, invocations.c.run_id == run["id"])
        .order_by(invocations.c.created_at.desc(), invocations.c.id),
        include_tools,
    )
    data["tools"] = [
        {
            "invocation": redact.ref("invocation", r["id"]),
            "attempt": redact.ref("attempt", r["attempt_id"]),
            "tool": redact.ref("tool", r["tool_id"]),
            "state": state(r["state"]),
            "approval_state": state(r["approval_state"]),
            **{
                k: number(r[k])
                for k in ("version", "expires_at", "created_at", "updated_at")
            },
        }
        for r in records
    ]
    records = await rows(
        "tool_decisions",
        select(
            *decision_columns(evidence),
            evidence.c.stage.label("action"),
            evidence.c.invocation_id,
            func.coalesce(
                invocations.c.attempt_id,
                evidence.c.decision["evidence"]["attempt_id"].as_string(),
            ).label("attempt_id"),
        )
        .outerjoin(
            invocations,
            and_(
                evidence.c.invocation_id == invocations.c.id,
                invocations.c.scope == scope,
            ),
        )
        .where(
            evidence.c.scope == scope,
            or_(
                invocations.c.run_id == run["id"],
                evidence.c.decision["evidence"]["run_id"].as_string() == run["id"],
            ),
        )
        .order_by(evidence.c.created_at.desc(), evidence.c.id),
        include_tools,
    )
    data["tool_decisions"] = [
        decision_row(r) | {"invocation": redact.ref("invocation", r["invocation_id"])}
        for r in records
    ]
    records = await rows(
        "artifacts",
        select(*(artifacts.c[k] for k in ("id", "attempt_id", "size", "created_at")))
        .where(artifacts.c.run_id == run["id"])
        .order_by(artifacts.c.created_at.desc(), artifacts.c.id),
        include_artifacts,
    )
    data["artifacts"] = [
        {
            "artifact": redact.ref("artifact", r["id"]),
            "attempt": redact.ref("attempt", r["attempt_id"]),
            "size": number(r["size"]),
            "created_at": number(r["created_at"]),
        }
        for r in records
        if await allowed("artifact", r["id"])
    ]
    records = await rows(
        "recovery",
        select(
            *(
                plans.c[k]
                for k in ("attempt_id", "state", "category", "retry_at", "updated_at")
            )
        ).where(plans.c.scope == scope, plans.c.run_id == run["id"]),
    )
    data["recovery"] = [
        {
            "attempt": redact.ref("attempt", r["attempt_id"]),
            "state": state(r["state"]),
            "category": r["category"]
            if r["category"] in set(CATEGORIES.values()) | {"authorization"}
            else "unknown",
            "retry_at": number(r["retry_at"]),
            "updated_at": number(r["updated_at"]),
        }
        for r in records
    ]
    records = await rows(
        "schedulers",
        select(loops)
        .where(loops.c.scope == scope, loops.c.service.in_(SERVICES))
        .order_by(loops.c.heartbeat_at.desc(), loops.c.service, loops.c.instance),
        include_operational,
    )
    data["schedulers"] = [
        {
            "service": r["service"],
            "instance": redact.ref("scheduler", r["instance"]),
            "diagnosis": classify(r, now),
            "error": redact.code(r["error"]),
            "evidence_level": "platform_recorded",
            **{
                k: number(r[k])
                for k in (
                    "heartbeat_at",
                    "cycle_started_at",
                    "completed_at",
                    "succeeded_at",
                    "duration_seconds",
                    "cycles",
                    "consecutive_errors",
                )
            },
        }
        for r in records
    ]
    records = await rows(
        "sync",
        select(projections.c.consumer, projections.c.version, projections.c.observed_at)
        .where(projections.c.run_id == run["id"])
        .order_by(projections.c.consumer),
        include_operational,
    )
    data["sync"] = [
        {
            "consumer": redact.ref("consumer", r["consumer"]),
            "version": r["version"],
            "observed_at": number(r["observed_at"]),
            "lag_versions": max(0, run["version"] - r["version"]),
            "state": "behind" if r["version"] < run["version"] else "current",
            "evidence_level": "platform_recorded",
        }
        for r in records
    ]
    data["observability"] = {
        "generated_at": now,
        "last_event_at": run["updated_at"],
        "scheduler_coverage": "bounded_scope_instances"
        if include_operational
        else "restricted",
        "sync_coverage": "local_receiver_projections"
        if include_operational
        else "restricted",
        "unregistered_remote_receiver": "unknown",
        "agent_internal_activity": "not_observed",
        "estimated_values": "not_synthesized",
    }
    for name in LIMITS:
        coverage[name]["visible_before_byte_limit"] = len(data[name])
        coverage[name]["evidence_level"] = "platform_recorded"
    coverage["attempts"]["evidence_level"] = "per_record"
    payload = encode(data)
    while len(payload) > MAX_BYTES - 32768:
        largest = max(LIMITS, key=lambda k: len(encode(data[k])))
        if not data[largest]:
            raise RuntimeErrorCode("DIAGNOSTIC_TOO_LARGE")
        data[largest].pop()
        coverage[largest]["has_more"] = coverage[largest]["size_trimmed"] = True
        payload = encode(data)
    for name in LIMITS:
        coverage[name]["exported_records"] = len(data[name])
    explanation = (
        "# DiOS diagnostic bundle\n\n"
        "Read diagnostic.json with manifest.json. Times are Unix seconds; null means unknown.\n"
        "The original Run identity and snapshot version are retained. Other identities use package-local pseudonyms.\n"
        "Coverage records permissions and row/byte truncation. Related rows outside these limits may be absent.\n"
        "Prompts, code, raw outputs, arguments, credentials, paths, subjects, approval text and artifact contents are excluded.\n"
        "Scheduler observations and receiver version lag are separate from model waiting. Missing receivers remain unknown.\n"
        "A stale observation or slow model does not prove task death. No operation is restarted by export.\n"
        "Manifest SHA-256 values verify exported bytes only; this projection does not verify the original audit chain.\n"
    ).encode()
    files = {"diagnostic.json": payload, "README.md": explanation}
    manifest = {
        "format": "dios-diagnostic-bundle",
        "version": 1,
        "redaction": "allowlist-v1",
        "generated_at": now,
        "run_id": run["id"],
        "run_version": run["version"],
        "coverage": coverage,
        "max_uncompressed_bytes": MAX_BYTES,
        "files": {
            name: {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in files.items()
        },
    }
    files["manifest.json"] = encode(manifest)
    if sum(map(len, files.values())) > MAX_BYTES:
        raise RuntimeErrorCode("DIAGNOSTIC_TOO_LARGE")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    result = buffer.getvalue()
    if len(result) > MAX_BYTES:
        raise RuntimeErrorCode("DIAGNOSTIC_TOO_LARGE")
    return result
