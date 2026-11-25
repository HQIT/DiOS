"""Idempotent, evidence-graded usage snapshots; no implied token/currency totals."""

import json
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from .database import RuntimeErrorCode
from .usage_schema import observations, require

TOKEN_FIELDS = {
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "thoughtTokens",
    "cachedReadTokens",
    "cachedWriteTokens",
}


def validate(frame):
    if not isinstance(frame, dict) or set(frame) != {
        "type",
        "session_id",
        "sequence",
        "kind",
        "quality",
        "values",
    }:
        raise RuntimeErrorCode("INVALID_USAGE")
    if (
        frame["type"] != "usage"
        or frame["kind"] not in {"acp_session", "acp_prompt"}
        or frame["quality"] not in {"reported", "invalid"}
    ):
        raise RuntimeErrorCode("INVALID_USAGE")
    if (
        not isinstance(frame["session_id"], str)
        or not 1 <= len(frame["session_id"]) <= 160
        or type(frame["sequence"]) is not int
        or not 1 <= frame["sequence"] <= 1000000
    ):
        raise RuntimeErrorCode("INVALID_USAGE")
    value = frame["values"]
    if not isinstance(value, dict):
        raise RuntimeErrorCode("INVALID_USAGE")
    if frame["quality"] == "invalid":
        if value:
            raise RuntimeErrorCode("INVALID_USAGE")
        return frame
    allowed = (
        {"used", "size", "cost"} if frame["kind"] == "acp_session" else TOKEN_FIELDS
    )
    required = (
        {"used", "size"}
        if frame["kind"] == "acp_session"
        else {"inputTokens", "outputTokens", "totalTokens"}
    )
    if set(value) - allowed or not required.issubset(value):
        raise RuntimeErrorCode("INVALID_USAGE")
    for key, v in value.items():
        if key == "cost":
            if (
                not isinstance(v, dict)
                or set(v) != {"amount", "unit"}
                or not isinstance(v["amount"], str)
                or len(v["amount"]) > 64
                or not isinstance(v["unit"], str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,15}", v["unit"])
            ):
                raise RuntimeErrorCode("INVALID_USAGE")
            try:
                amount = Decimal(v["amount"])
                if not amount.is_finite() or not 0 <= amount <= 10**12:
                    raise ValueError()
            except (ValueError, InvalidOperation):
                raise RuntimeErrorCode("INVALID_USAGE") from None
        elif type(v) is not int or not 0 <= v <= 10**15:
            raise RuntimeErrorCode("INVALID_USAGE")
    return frame


def extract(data):
    """Recover complete usage frames from bounded stdout even without a final result."""
    session = None
    result = []
    for raw in data.split(b"\n")[:-1]:
        if len(raw) > 65536:
            continue
        try:
            frame = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(frame, dict):
            continue
        if frame.get("type") == "session":
            if session is not None:
                break
            session = frame.get("session_id")
        if (
            frame.get("type") == "usage"
            and session
            and frame.get("session_id") == session
        ):
            try:
                result.append(validate(frame))
            except RuntimeErrorCode:
                continue
    return result


async def record(c, scope, run, attempt, frames, now, evidence):
    from .store import digest

    await require(c)
    for frame in frames:
        validate(frame)
        if not attempt["session_id"] or frame["session_id"] != attempt["session_id"]:
            raise RuntimeErrorCode("SESSION_CHANGED")
        old = (
            (
                await c.execute(
                    select(observations).where(
                        observations.c.scope == scope,
                        observations.c.attempt_id == attempt["id"],
                        observations.c.sequence == frame["sequence"],
                    )
                )
            )
            .mappings()
            .first()
        )
        key = digest(frame)
        if old:
            if old["digest"] != key:
                raise RuntimeErrorCode("USAGE_CONFLICT")
            linked = {**evidence, **old["evidence"]}
            if linked != old["evidence"]:
                await c.execute(
                    observations.update()
                    .where(
                        observations.c.scope == scope,
                        observations.c.attempt_id == attempt["id"],
                        observations.c.sequence == frame["sequence"],
                    )
                    .values(evidence=linked)
                )
            continue
        await c.execute(
            observations.insert().values(
                scope=scope,
                run_id=run["id"],
                attempt_id=attempt["id"],
                session_id=frame["session_id"],
                sequence=frame["sequence"],
                kind=frame["kind"],
                value=frame,
                digest=key,
                evidence=evidence,
                recorded_at=now,
            )
        )


async def view(c, scope, run_id):
    await require(c)
    # Latest snapshot of each kind per Attempt, not a sum of cumulative snapshots.
    ranked = (
        select(
            observations,
            func.row_number()
            .over(
                partition_by=(observations.c.attempt_id, observations.c.kind),
                order_by=observations.c.sequence.desc(),
            )
            .label("rank"),
        )
        .where(observations.c.scope == scope, observations.c.run_id == run_id)
        .subquery()
    )
    rows = (
        (
            await c.execute(
                select(ranked)
                .where(ranked.c.rank == 1)
                .order_by(ranked.c.recorded_at.desc(), ranked.c.attempt_id)
                .limit(101)
            )
        )
        .mappings()
        .all()
    )
    return {
        "evidence_level": "agent_reported",
        "settlement": "unreconciled",
        "at_limit": len(rows) > 100,
        "items": [
            {
                "attempt_id": x["attempt_id"],
                "session_id": x["session_id"],
                "sequence": x["sequence"],
                "kind": x["kind"],
                "quality": x["value"]["quality"],
                "values": x["value"]["values"],
                "recorded_at": x["recorded_at"],
                "evidence": x["evidence"],
                "aggregation": "session_snapshot"
                if x["kind"] == "acp_session"
                else "unspecified_acp_scope",
            }
            for x in rows[:100]
        ],
    }
