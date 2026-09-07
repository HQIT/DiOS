"""事件投递：通过 A2A 协议把事件转化为 Agent message/send 调用。

职责：
- 事件去重与重试（EventLog 层面）
- 订阅匹配结果的 fan-out：为每个匹配 Agent 调 a2a_service.send_message
- 不再直接操作容器；容器生命周期由 a2a_service 管理

EventLog 关注"事件本身"，A2ATask 关注"Agent 调用"，两者通过 context_id=event_log.id 关联。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import Agent, EventDedupClaim, EventLog
from app.services.e2ag import Decision, append_audit_entry, evaluate_contract, evaluate_policy
from app.services.event_normalizer import CloudEvent, compute_dedup_hash
from app.services.metrics import metrics
from app.services import a2a_service

logger = logging.getLogger(__name__)


async def _claim_dedup_hash(
    db: AsyncSession,
    dedup_hash: str,
    *,
    expires_at: datetime,
) -> tuple[bool, str, str]:
    """Atomically claim a dedup hash, including safe takeover after expiry.

    Returns ``(claimed, duplicate_event_log_id, owner_token)``.  The unique
    claim row closes the check-then-insert race across independent sessions.
    """
    owner_token = uuid.uuid4().hex
    values = {
        "dedup_hash": dedup_hash,
        "owner_token": owner_token,
        "event_log_id": "",
        "expires_at": expires_at,
    }
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        statement = insert(EventDedupClaim).values(**values).on_conflict_do_nothing(
            index_elements=[EventDedupClaim.dedup_hash]
        )
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        statement = insert(EventDedupClaim).values(**values).on_conflict_do_nothing(
            index_elements=[EventDedupClaim.dedup_hash]
        )
    else:
        # DiOS currently ships with SQLite.  Keeping the fallback explicit avoids
        # silently claiming cross-database atomicity that has not been exercised.
        raise RuntimeError(f"E2AG_DEDUP_DIALECT_UNSUPPORTED:{dialect}")

    inserted = await db.execute(statement)
    if inserted.rowcount == 1:
        return True, "", owner_token

    now = datetime.now(timezone.utc)
    reclaimed = await db.execute(
        update(EventDedupClaim)
        .where(
            EventDedupClaim.dedup_hash == dedup_hash,
            EventDedupClaim.expires_at <= now,
        )
        .values(
            owner_token=owner_token,
            event_log_id="",
            expires_at=expires_at,
        )
    )
    if reclaimed.rowcount == 1:
        return True, "", owner_token

    existing = await db.get(EventDedupClaim, dedup_hash)
    return False, (existing.event_log_id if existing else ""), owner_token


async def dispatch_event(
    event: CloudEvent,
    agent_ids: list[str],
    db: AsyncSession,
    is_retry: bool = False,
    original_log_id: str | None = None,
) -> tuple[EventLog | None, str | None]:
    """将事件投递给匹配的 Agent 列表，创建 EventLog 记录。
    
    Args:
        event: CloudEvent 格式的事件
        agent_ids: 匹配的 Agent ID 列表
        db: 数据库会话
        is_retry: 是否为重试操作（跳过去重检查）
        original_log_id: 原始事件 log ID（重试时使用）
    
    Returns:
        (EventLog, error_message) 或 (None, error) 如果是去重
    """
    start_time = time.time()
    event_type = event.get("type", "")
    event_source = event.get("source", "")
    trace_id = uuid.uuid4().hex
    e2ag_mode = str(getattr(settings, "e2ag_mode", "enforce") or "enforce").lower()
    if e2ag_mode not in {"off", "contract", "enforce"}:
        logger.warning("Unknown E2AG mode %s; falling back to enforce", e2ag_mode)
        e2ag_mode = "enforce"
    
    # 记录指标
    metrics.record_event_received(event_type)
    if is_retry:
        metrics.record_retry()
    
    # 1. E2AG contract admission + target-scoped policy gate.
    target_capabilities: dict[str, dict] = {}
    if agent_ids:
        agents_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
        target_capabilities = {
            agent.id: (agent.capabilities or {})
            for agent in agents_result.scalars().all()
        }
    if e2ag_mode == "off":
        contract = Decision(
            stage="contract", decision="allow", reason_codes=("E2AG_DISABLED",)
        )
        policy = Decision(
            stage="policy", decision="allow", reason_codes=("E2AG_DISABLED",)
        )
    else:
        contract = evaluate_contract(event)
        policy = (
            Decision(
                stage="policy",
                decision="allow",
                reason_codes=("POLICY_DISABLED_CONTRACT_MODE",),
                contract_type=contract.contract_type,
            )
            if e2ag_mode == "contract"
            else evaluate_policy(event, contract, target_capabilities)
        )
    effective_decision = contract.decision if e2ag_mode == "contract" else policy.decision
    event_status = "denied" if effective_decision == "deny" else effective_decision
    audit_chain = append_audit_entry(
        [], trace_id=trace_id, stage="contract", outcome=contract.decision,
        evidence=contract.as_dict(),
    )
    audit_chain = append_audit_entry(
        audit_chain, trace_id=trace_id, stage="policy", outcome=effective_decision,
        evidence=policy.as_dict(),
    )

    # 2. 去重检查（非重试操作才检查）
    dedup_hash = compute_dedup_hash(event)
    
    dedup_claim_owner = ""
    if not is_retry and getattr(settings, "event_dedup_enabled", True):
        # 检查去重排除列表
        exclude_types = getattr(settings, "event_dedup_exclude_types", ["cron.tick", "manual.trigger"])
        if event_type not in exclude_types:
            # 查询最近时间窗口内是否有重复事件
            dedup_window_hours = getattr(settings, "event_dedup_window_hours", 1)
            claimed, duplicate_id, dedup_claim_owner = await _claim_dedup_hash(
                db,
                dedup_hash,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=dedup_window_hours),
            )
            if not claimed:
                logger.info(
                    "Duplicate event detected (hash=%s), original event_id=%s",
                    dedup_hash[:8], duplicate_id or "pending-claim"
                )
                metrics.record_dedup()
                return None, f"Duplicate of event {duplicate_id or 'pending-claim'}"
    
    # 3. Audit every decision, including denied and approval-held events.
    if is_retry and original_log_id:
        event_log = await db.get(EventLog, original_log_id)
        if not event_log:
            logger.error("Original event log %s not found for retry", original_log_id)
            return None, "Original event log not found"
    else:
        event_log = EventLog(
            source=event.get("source", ""),
            event_type=event_type,
            subject=event.get("subject", ""),
            cloud_event=event,
            matched_agent_ids=agent_ids,
            status="received" if effective_decision == "allow" else event_status,
            trace_id=trace_id,
            contract_decision={"mode": e2ag_mode, **contract.as_dict()},
            policy_decision={"mode": e2ag_mode, **policy.as_dict(), "effective_decision": effective_decision},
            audit_chain=audit_chain,
            dedup_hash=dedup_hash,
            retry_count=0,
            max_retries=getattr(settings, "event_max_retries", 3),
            next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        db.add(event_log)
        if dedup_claim_owner:
            await db.flush()
            claim = await db.get(EventDedupClaim, dedup_hash)
            if claim is None or claim.owner_token != dedup_claim_owner:
                raise RuntimeError("E2AG_DEDUP_CLAIM_LOST")
            claim.event_log_id = event_log.id
        await db.commit()
        await db.refresh(event_log)

    if effective_decision != "allow":
        reason_codes = contract.reason_codes if effective_decision == "deny" and not contract.permits_execution else policy.reason_codes
        error_detail = ",".join(reason_codes)
        event_log.error_message = error_detail
        event_log.audit_chain = append_audit_entry(
            event_log.audit_chain,
            trace_id=event_log.trace_id,
            stage="dispatch",
            outcome="blocked",
            evidence={"reason_codes": list(reason_codes)},
        )
        event_log.next_retry_at = None
        if effective_decision == "approval_required" and not is_retry:
            from app.services.e2ag_approval import create_approval

            await create_approval(db, event_log)
        await db.commit()
        await db.refresh(event_log)
        metrics.record_dispatch(time.time() - start_time, False, agent_ids)
        logger.info(
            "E2AG held event: id=%s trace_id=%s decision=%s reasons=%s",
            event_log.id, event_log.trace_id, effective_decision, error_detail,
        )
        return event_log, error_detail

    if not agent_ids:
        event_log.status = "dispatched"  # 无匹配 Agent，视为完成
        event_log.audit_chain = append_audit_entry(
            event_log.audit_chain,
            trace_id=event_log.trace_id,
            stage="dispatch",
            outcome="no_target",
            evidence={"matched_agent_ids": []},
        )
        await db.commit()
        return event_log, None

    # 4. 把 CloudEvent 转为 A2A Message，逐个 fan-out 投递
    a2a_message = a2a_service.cloudevent_to_a2a_message(event, trace_id=event_log.trace_id)
    logger.info(
        "Dispatch event start: id=%s type=%s source=%s matched_agents=%s",
        event_log.id, event_type, event_source, agent_ids,
    )

    dispatched = False
    reasons: list[str] = []

    for agent_id in agent_ids:
        try:
            task = await a2a_service.send_message(
                db=db,
                agent_id=agent_id,
                message=a2a_message,
                context_id=event_log.id,
                trace_id=event_log.trace_id,
            )
            if task.status in ("failed",):
                reasons.append(f"Agent {agent_id}: {task.error}")
                logger.warning("A2A dispatch to agent %s returned failed: %s", agent_id, task.error)
            else:
                dispatched = True
                event_log.audit_chain = append_audit_entry(
                    event_log.audit_chain,
                    trace_id=event_log.trace_id,
                    stage="a2a_task",
                    outcome=task.status,
                    evidence={"task_id": task.id, "agent_id": agent_id},
                )
                logger.info(
                    "Dispatched event %s(type=%s) to agent %s via A2A (task=%s status=%s context=%s)",
                    event_log.id, event_type, agent_id, task.id, task.status, task.context_id,
                )
        except Exception as e:
            reasons.append(f"Agent {agent_id}: {type(e).__name__}: {e}")
            logger.exception("A2A send_message failed for agent %s", agent_id)

    # 5. 更新 EventLog 状态
    event_log.status = "dispatched" if dispatched else "failed"
    error_detail = "; ".join(reasons) if reasons else None

    if not dispatched:
        event_log.error_message = error_detail or "Failed to dispatch to any agent"

    event_log.audit_chain = append_audit_entry(
        event_log.audit_chain,
        trace_id=event_log.trace_id,
        stage="dispatch",
        outcome=event_log.status,
        evidence={"matched_agent_ids": agent_ids, "error": error_detail or ""},
    )

    await db.commit()
    await db.refresh(event_log)

    # 6. 记录性能指标
    duration = time.time() - start_time
    metrics.record_dispatch(duration, dispatched, agent_ids)
    logger.info(
        "Dispatch event done: id=%s type=%s status=%s duration_ms=%.0f error=%s",
        event_log.id, event_type, event_log.status, duration * 1000, error_detail or "",
    )

    return event_log, error_detail


async def resume_approved_event(event_log: EventLog, db: AsyncSession) -> None:
    """Resume the original fan-out after a single E2AG human approval."""
    if event_log.status != "approval_approved":
        raise ValueError("APPROVAL_EVENT_NOT_APPROVED")
    if not event_log.matched_agent_ids:
        event_log.status = "dispatched"
        event_log.audit_chain = append_audit_entry(
            event_log.audit_chain or [], trace_id=event_log.trace_id,
            stage="dispatch", outcome="no_target_after_approval",
            evidence={"matched_agent_ids": []},
        )
        await db.commit()
        return

    message = a2a_service.cloudevent_to_a2a_message(
        event_log.cloud_event, trace_id=event_log.trace_id,
    )
    dispatched = False
    reasons: list[str] = []
    for agent_id in event_log.matched_agent_ids:
        try:
            task = await a2a_service.send_message(
                db=db,
                agent_id=agent_id,
                message=message,
                context_id=event_log.id,
                trace_id=event_log.trace_id,
            )
            if task.status == "failed":
                reasons.append(f"Agent {agent_id}: {task.error}")
                continue
            dispatched = True
            event_log.audit_chain = append_audit_entry(
                event_log.audit_chain or [], trace_id=event_log.trace_id,
                stage="a2a_task", outcome=task.status,
                evidence={"task_id": task.id, "agent_id": agent_id, "after_approval": True},
            )
        except Exception as exc:
            reasons.append(f"Agent {agent_id}: {type(exc).__name__}: {exc}")
    event_log.status = "dispatched" if dispatched else "failed"
    event_log.error_message = "; ".join(reasons)
    event_log.audit_chain = append_audit_entry(
        event_log.audit_chain or [], trace_id=event_log.trace_id,
        stage="dispatch", outcome=event_log.status,
        evidence={
            "matched_agent_ids": event_log.matched_agent_ids,
            "after_approval": True,
            "error": event_log.error_message,
        },
    )
    await db.commit()
