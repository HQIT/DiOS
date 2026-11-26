"""Quarantine authenticated stale messages without changing execution ownership."""

from sqlalchemy import select

from app.services.e2ag import Decision

from .database import RuntimeErrorCode
from .remote_schema import uploads
from .store import ACTIVE_ATTEMPTS, bounded, digest
from .work_auth_schema import decisions

ACTION = "remote-quarantine"


async def retain(c, scope, envelope, run, attempt, method, params, now):
    """The caller must authenticate the exact historical Worker/Attempt binding first."""
    if method == "report":
        value = params.get("report")
        allowed = {
            "sequence",
            "phase",
            "session_id",
            "process_id",
            "process_instance",
            "remote",
            "output",
            "output_observed_at",
            "authorization_epoch",
            "output_excerpt",
            "usage",
        }
        if (
            set(params) != {"binding", "report"}
            or not isinstance(value, dict)
            or set(value) - allowed
            or type(value.get("sequence")) is not int
            or value["sequence"] < 1
            or not isinstance(value.get("phase"), str)
            or value["phase"] not in ACTIVE_ATTEMPTS
        ):
            return False
    elif method == "complete":
        if set(params) != {"binding", "key"} or not isinstance(params["key"], str):
            return False
        # An arbitrary hash never grants access to another Attempt's contents.
        row = (
            (
                await c.execute(
                    select(uploads).where(
                        uploads.c.scope == scope,
                        uploads.c.attempt_id == attempt["id"],
                        uploads.c.key == params["key"],
                        uploads.c.complete.is_(True),
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        value = {"key": row["key"], "size": row["size"], "upload_id": row["id"]}
    else:
        return False
    try:
        bounded(value)
    except (RuntimeErrorCode, ValueError, TypeError):
        return False
    message_digest = digest([method, params["binding"], value])
    identity = digest([ACTION, scope, message_digest])
    if (
        await c.execute(select(decisions.c.id).where(decisions.c.id == identity))
    ).first():
        return True
    evidence = {
        "method": method,
        "message_digest": message_digest,
        "worker_id": attempt["worker_id"],
        "worker_instance": attempt["worker_instance"],
        "epoch": attempt["epoch"],
        "current_epoch": run["epoch"],
        "current_attempt_id": run["attempt_id"],
        "payload": value,
        "certificate_sha256": envelope["certificate"],
        "disposition": "quarantined",
        "accepted_as_result": False,
    }
    await c.execute(
        decisions.insert().values(
            id=identity,
            scope=scope,
            run_id=run["id"],
            attempt_id=attempt["id"],
            action=ACTION,
            created_at=now,
            decision=Decision(
                ACTION, "deny", ("STALE_OWNER",), evidence=evidence
            ).as_dict(),
        )
    )
    return True
