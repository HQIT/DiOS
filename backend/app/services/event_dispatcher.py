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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import EventLog
from app.services.event_normalizer import CloudEvent, compute_dedup_hash
from app.services.metrics import metrics
from app.services import a2a_service

logger = logging.getLogger(__name__)


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
    
    # 记录指标
    metrics.record_event_received(event_type)
    if is_retry:
        metrics.record_retry()
    
    # 1. 去重检查（非重试操作才检查）
    dedup_hash = compute_dedup_hash(event)
    
    if not is_retry and getattr(settings, "event_dedup_enabled", True):
        # 检查去重排除列表
        exclude_types = getattr(settings, "event_dedup_exclude_types", ["cron.tick", "manual.trigger"])
        if event_type not in exclude_types:
            # 查询最近时间窗口内是否有重复事件
            dedup_window_hours = getattr(settings, "event_dedup_window_hours", 1)
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=dedup_window_hours)
            
            existing = await db.execute(
                select(EventLog).where(
                    EventLog.dedup_hash == dedup_hash,
                    EventLog.created_at > cutoff_time,
                ).limit(1)
            )
            
            duplicate = existing.scalar_one_or_none()
            if duplicate:
                logger.info(
                    "Duplicate event detected (hash=%s), original event_id=%s",
                    dedup_hash[:8], duplicate.id
                )
                metrics.record_dedup()
                return None, f"Duplicate of event {duplicate.id}"
    
    # 2. 创建 EventLog 记录（如果是重试，更新原记录而不是创建新记录）
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
            status="received",
            dedup_hash=dedup_hash,
            retry_count=0,
            max_retries=getattr(settings, "event_max_retries", 3),
            next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        db.add(event_log)
        await db.commit()
        await db.refresh(event_log)

    if not agent_ids:
        event_log.status = "dispatched"  # 无匹配 Agent，视为完成
        await db.commit()
        return event_log, None

    # 3. 把 CloudEvent 转为 A2A Message，逐个 fan-out 投递
    a2a_message = a2a_service.cloudevent_to_a2a_message(event)

    dispatched = False
    reasons: list[str] = []

    for agent_id in agent_ids:
        try:
            task = await a2a_service.send_message(
                db=db,
                agent_id=agent_id,
                message=a2a_message,
                context_id=event_log.id,
            )
            if task.status in ("failed",):
                reasons.append(f"Agent {agent_id}: {task.error}")
                logger.warning("A2A dispatch to agent %s returned failed: %s", agent_id, task.error)
            else:
                dispatched = True
                logger.info(
                    "Dispatched event %s to agent %s via A2A (task %s, status=%s)",
                    event_log.id, agent_id, task.id, task.status,
                )
        except Exception as e:
            reasons.append(f"Agent {agent_id}: {type(e).__name__}: {e}")
            logger.exception("A2A send_message failed for agent %s", agent_id)

    # 4. 更新 EventLog 状态
    event_log.status = "dispatched" if dispatched else "failed"
    error_detail = "; ".join(reasons) if reasons else None

    if not dispatched:
        event_log.error_message = error_detail or "Failed to dispatch to any agent"

    await db.commit()
    await db.refresh(event_log)

    # 5. 记录性能指标
    duration = time.time() - start_time
    metrics.record_dispatch(duration, dispatched, agent_ids)

    return event_log, error_detail
