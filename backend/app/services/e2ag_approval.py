"""Auditable single-transition approval workflow for E2AG-held events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import E2AGApproval, EventLog
from app.services.e2ag import append_audit_entry


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def create_approval(db: AsyncSession, event_log: EventLog) -> E2AGApproval:
    approval = E2AGApproval(
        event_log_id=event_log.id,
        trace_id=event_log.trace_id,
        status="pending",
        expires_at=_now() + timedelta(seconds=settings.e2ag_approval_ttl_seconds),
    )
    db.add(approval)
    await db.flush()
    event_log.audit_chain = append_audit_entry(
        event_log.audit_chain or [],
        trace_id=event_log.trace_id,
        stage="approval",
        outcome="pending",
        evidence={"approval_id": approval.id, "expires_at": approval.expires_at.isoformat()},
    )
    await db.commit()
    await db.refresh(approval)
    return approval


async def decide_approval(
    db: AsyncSession,
    approval_id: str,
    *,
    decision: str,
    actor: str,
    reason: str = "",
) -> tuple[E2AGApproval, EventLog]:
    """Consume a pending approval exactly once, then resume only if approved."""
    if decision not in {"approved", "rejected"}:
        raise ValueError("APPROVAL_DECISION_INVALID")
    approval = await db.get(E2AGApproval, approval_id)
    if approval is None:
        raise LookupError("APPROVAL_NOT_FOUND")
    event_log = await db.get(EventLog, approval.event_log_id)
    if event_log is None:
        raise LookupError("APPROVAL_EVENT_NOT_FOUND")
    now = _now()
    if approval.status != "pending":
        raise ValueError("APPROVAL_ALREADY_DECIDED")
    if _aware(approval.expires_at) <= now:
        approval.status = "expired"
        approval.decided_at = now
        event_log.status = "approval_expired"
        event_log.audit_chain = append_audit_entry(
            event_log.audit_chain or [], trace_id=event_log.trace_id,
            stage="approval", outcome="expired",
            evidence={"approval_id": approval.id},
        )
        await db.commit()
        raise ValueError("APPROVAL_EXPIRED")

    claimed = await db.execute(
        update(E2AGApproval)
        .where(E2AGApproval.id == approval_id, E2AGApproval.status == "pending")
        .values(status=decision, decided_at=now, actor=actor, reason=reason)
    )
    if claimed.rowcount != 1:
        await db.rollback()
        raise ValueError("APPROVAL_ALREADY_DECIDED")
    await db.refresh(approval)
    event_log.status = f"approval_{decision}"
    event_log.next_retry_at = None
    event_log.audit_chain = append_audit_entry(
        event_log.audit_chain or [], trace_id=event_log.trace_id,
        stage="approval", outcome=decision,
        evidence={"approval_id": approval.id, "actor": actor, "reason": reason},
    )
    await db.commit()

    if decision == "approved":
        from app.services.event_dispatcher import resume_approved_event

        await resume_approved_event(event_log, db)
        await db.refresh(event_log)
    return approval, event_log
