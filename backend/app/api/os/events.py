"""Event Gateway API: webhook 接收 + 手动触发 + 事件目录 + 事件日志查询。"""

from datetime import datetime, timedelta, timezone
from pydantic import BaseModel
from fastapi import APIRouter, Depends, Request, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.models.tables import Subscription, EventLog, Connector, A2ATask, Agent
from app.models.schemas import EventLogOut, EventActivityOverviewOut, EventActivityItemOut
from app.services.event_normalizer import (
    detect_and_normalize,
    get_event_catalog,
    _make_event,
)
from app.services.event_router import match_subscriptions
from app.services.event_dispatcher import dispatch_event

router = APIRouter(prefix="/events", tags=["events"])


async def _webhook_secrets(db: AsyncSession) -> dict[str, str]:
    """优先从 Connector 表取 webhook secret，否则用 settings。"""
    out = dict(settings.webhook_secrets)
    result = await db.execute(
        select(Connector).where(
            Connector.enabled == True,  # noqa: E712
            Connector.type.in_(["github", "gitlab", "gitea", "git_webhook"]),
        )
    )
    for c in result.scalars().all():
        secret = (c.config or {}).get("secret") or ""
        if not secret:
            continue
        if c.type == "git_webhook":
            platform = (c.config or {}).get("platform", "")
            if platform:
                out[platform] = secret
        else:
            out[c.type] = secret
    return out


class ManualEventBody(BaseModel):
    event_type: str
    source: str = "manual/test"
    subject: str = ""
    data: dict = {}


@router.post("/webhook/{source}")
async def receive_webhook(
    source: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """接收外部 webhook，自动识别平台、标准化、匹配订阅、投递。"""
    body = await request.body()
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    headers = dict(request.headers)
    secrets = await _webhook_secrets(db)
    try:
        event = detect_and_normalize(headers, payload, body, secrets)
    except ValueError as e:
        raise HTTPException(403, str(e))

    subs_result = await db.execute(
        select(Subscription).where(Subscription.enabled == True)  # noqa: E712
    )
    subscriptions = list(subs_result.scalars().all())

    matched_ids = match_subscriptions(event, subscriptions)

    event_log, error_detail = await dispatch_event(event, matched_ids, db)
    if event_log is None:
        # 去重命中：不入库新日志，返回可读结果，避免 500
        return {
            "event_id": None,
            "type": event.get("type"),
            "source": event.get("source"),
            "matched_agents": matched_ids,
            "status": "deduplicated",
            "error": error_detail,
        }

    return {
        "event_id": event_log.id,
        "type": event.get("type"),
        "source": event.get("source"),
        "matched_agents": matched_ids,
        "status": event_log.status,
        "error": error_detail,
    }


@router.get("/catalog")
async def event_catalog(db: AsyncSession = Depends(get_db)):
    """返回系统支持的所有事件源和事件类型，附带各 source 的 Connector 配置状态。"""
    catalog = get_event_catalog()

    result = await db.execute(
        select(Connector).where(Connector.enabled == True)  # noqa: E712
    )
    connectors = list(result.scalars().all())

    configured: set[str] = set()
    for c in connectors:
        if c.type in ("git_webhook", "github", "gitlab", "gitea"):
            configured.add("git")
        elif c.type == "imap":
            configured.add("email")
        elif c.type == "generic":
            configured.add("webhook")

    configured.update(["manual", "cron"])

    connector_status = {s["id"]: s["id"] in configured for s in catalog["sources"]}
    catalog["connector_status"] = connector_status
    return catalog


@router.post("/manual")
async def trigger_manual_event(
    body: ManualEventBody,
    db: AsyncSession = Depends(get_db),
):
    """手动触发一个事件，用于测试/模拟。"""
    event = _make_event(
        source=body.source,
        event_type=body.event_type,
        subject=body.subject,
        data=body.data,
    )

    subs_result = await db.execute(
        select(Subscription).where(Subscription.enabled == True)  # noqa: E712
    )
    subscriptions = list(subs_result.scalars().all())

    matched_ids = match_subscriptions(event, subscriptions)

    event_log, error_detail = await dispatch_event(event, matched_ids, db)
    if event_log is None:
        return {
            "event_id": None,
            "type": event.get("type"),
            "source": event.get("source"),
            "matched_agents": matched_ids,
            "status": "deduplicated",
            "error": error_detail,
        }

    return {
        "event_id": event_log.id,
        "type": event.get("type"),
        "source": event.get("source"),
        "matched_agents": matched_ids,
        "status": event_log.status,
        "error": error_detail,
    }


@router.get("", response_model=dict)
async def list_events(
    source: str | None = Query(None),
    event_type: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """查询事件日志，支持分页。"""
    from sqlalchemy import func
    
    # 构建基础查询
    filters = []
    if source:
        filters.append(EventLog.source.contains(source))
    if event_type:
        filters.append(EventLog.event_type == event_type)
    if status:
        filters.append(EventLog.status == status)
    
    # 查总数
    count_stmt = select(func.count(EventLog.id))
    if filters:
        count_stmt = count_stmt.where(*filters)
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()
    
    # 查数据
    query = select(EventLog).order_by(EventLog.created_at.desc())
    if filters:
        query = query.where(*filters)
    query = query.limit(limit).offset(offset)
    
    result = await db.execute(query)
    items = result.scalars().all()
    
    return {
        "items": [EventLogOut.model_validate(item) for item in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/activity-gantt", response_model=dict)
async def get_activity_gantt(
    since_minutes: int = Query(60, ge=1, le=7 * 24 * 60),
    date: str | None = Query(None, description="按单日过滤，格式 YYYY-MM-DD"),
    agent_ids: str | None = Query(None, description="逗号分隔的 agent id 列表"),
    limit: int = Query(500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    range_start = now - timedelta(minutes=since_minutes)
    range_end = now
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "Invalid date format, expected YYYY-MM-DD")
        range_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        range_end = range_start + timedelta(days=1)

    stmt = (
        select(A2ATask, Agent.name, EventLog.id, EventLog.event_type, EventLog.source)
        .join(Agent, Agent.id == A2ATask.agent_id)
        .outerjoin(EventLog, EventLog.id == A2ATask.context_id)
        .where(A2ATask.created_at >= range_start, A2ATask.created_at < range_end)
        .order_by(A2ATask.created_at.asc())
        .limit(limit)
    )

    parsed_ids = [x.strip() for x in (agent_ids or "").split(",") if x.strip()]
    if parsed_ids:
        stmt = stmt.where(A2ATask.agent_id.in_(parsed_ids))

    rows = (await db.execute(stmt)).all()
    bars: list[dict] = []
    agents_map: dict[str, dict] = {}
    timeline_start: datetime | None = None
    timeline_end: datetime | None = None

    for task, agent_name, event_id, event_type, event_source in rows:
        start_at = task.created_at
        running = task.status in ("submitted", "working")
        end_at = None if running else task.updated_at
        effective_end = now if running else (task.updated_at or task.created_at)
        duration_ms = max(0, int((effective_end - start_at).total_seconds() * 1000))

        behaviors = [{"type": "start", "at": start_at.isoformat(), "label": "开始"}]
        if running:
            behaviors.append({"type": "running", "at": effective_end.isoformat(), "label": "运行中"})
        if not running and end_at is not None:
            end_label = "结束" if task.status == "completed" else task.status
            behaviors.append({"type": "end", "at": end_at.isoformat(), "label": end_label})
        if task.error:
            behaviors.append({"type": "error", "at": (task.updated_at or effective_end).isoformat(), "label": "失败"})
        if len(task.artifacts or []) > 0:
            behaviors.append({"type": "artifact", "at": (task.updated_at or effective_end).isoformat(), "label": "产出"})

        bars.append({
            "task_id": task.id,
            "agent_id": task.agent_id,
            "agent_name": agent_name or task.agent_id,
            "status": task.status,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat() if end_at else None,
            "effective_end_at": effective_end.isoformat(),
            "duration_ms": duration_ms,
            "event_id": event_id or "",
            "event_type": event_type or "",
            "source": event_source or "",
            "error": task.error or "",
            "artifacts_count": len(task.artifacts or []),
            "behaviors": behaviors,
        })

        if task.agent_id not in agents_map:
            agents_map[task.agent_id] = {"agent_id": task.agent_id, "agent_name": agent_name or task.agent_id, "task_count": 0}
        agents_map[task.agent_id]["task_count"] += 1

        if timeline_start is None or start_at < timeline_start:
            timeline_start = start_at
        if timeline_end is None or effective_end > timeline_end:
            timeline_end = effective_end

    if timeline_start is None:
        timeline_start = range_start
    if timeline_end is None:
        timeline_end = range_end

    agents = sorted(agents_map.values(), key=lambda x: x["agent_name"])
    return {
        "timeline_start": timeline_start.isoformat(),
        "timeline_end": timeline_end.isoformat(),
        "since_minutes": since_minutes,
        "date": date or "",
        "agents": agents,
        "bars": bars,
    }


@router.get("/{event_id}/activity-overview", response_model=EventActivityOverviewOut)
async def get_event_activity_overview(event_id: str, db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    from app.models.tables import A2ATask, Agent

    event_log = await db.get(EventLog, event_id)
    if not event_log:
        raise HTTPException(404, "Event not found")

    task_result = await db.execute(
        select(A2ATask)
        .where(A2ATask.context_id == event_id)
        .order_by(A2ATask.created_at.asc())
    )
    tasks = list(task_result.scalars().all())

    agent_name_map: dict[str, str] = {}
    if tasks:
        agent_ids = list({t.agent_id for t in tasks if t.agent_id})
        if agent_ids:
            agent_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
            agents = list(agent_result.scalars().all())
            agent_name_map = {a.id: a.name for a in agents}

    now = datetime.now(timezone.utc)
    timeline_start = event_log.created_at
    timeline_end = event_log.created_at
    items: list[EventActivityItemOut] = []

    for t in tasks:
        started_at = t.created_at
        is_running = t.status in ("submitted", "working")
        ended_at = None if is_running else t.updated_at
        effective_end = now if is_running else (t.updated_at or t.created_at)
        duration_ms = max(0, int((effective_end - started_at).total_seconds() * 1000))

        if started_at < timeline_start:
            timeline_start = started_at
        if effective_end > timeline_end:
            timeline_end = effective_end

        items.append(
            EventActivityItemOut(
                task_id=t.id,
                agent_id=t.agent_id,
                agent_name=agent_name_map.get(t.agent_id, t.agent_id),
                status=t.status,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                error=t.error or "",
                artifacts_count=len(t.artifacts or []),
            )
        )

    if timeline_end < timeline_start:
        timeline_end = timeline_start

    return EventActivityOverviewOut(
        event_id=event_log.id,
        event_type=event_log.event_type,
        source=event_log.source,
        status=event_log.status,
        timeline_start=timeline_start,
        timeline_end=timeline_end,
        items=items,
    )


@router.get("/{event_id}", response_model=EventLogOut)
async def get_event(event_id: str, db: AsyncSession = Depends(get_db)):
    event_log = await db.get(EventLog, event_id)
    if not event_log:
        raise HTTPException(404, "Event not found")
    return event_log


@router.post("/{event_id}/retry")
async def retry_event_manually(event_id: str, db: AsyncSession = Depends(get_db)):
    """手动重试失败的事件（运维操作）"""
    from datetime import datetime, timezone
    
    event_log = await db.get(EventLog, event_id)
    if not event_log:
        raise HTTPException(404, "Event not found")
    
    if event_log.status not in ("failed", "dead_letter"):
        raise HTTPException(400, f"Event is not in failed state (current: {event_log.status})")
    
    # 重置重试计数，重新投递
    event_log.retry_count = 0
    event_log.status = "failed"
    event_log.next_retry_at = datetime.now(timezone.utc)
    event_log.error_message = ""
    await db.commit()
    
    return {"message": "Event scheduled for retry", "event_id": event_id}


@router.get("/system/metrics")
async def get_metrics():
    """返回系统运行指标"""
    from app.services.metrics import metrics
    return metrics.get_summary()
