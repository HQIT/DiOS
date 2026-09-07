"""Agent 事件订阅管理 CRUD。"""

from fnmatch import fnmatch

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.tables import Agent, Subscription, Connector
from app.models.schemas import SubscriptionCreate, SubscriptionUpdate, SubscriptionOut
from app.services.connector_capabilities import build_source_pattern_items
from app.services.event_normalizer import get_event_catalog

router = APIRouter(tags=["subscriptions"])


# ── 全局订阅列表（拓扑图用） ──

_VALID_EVENT_TYPES = {item["type"] for item in get_event_catalog()["event_types"]}


def _validate_event_types(event_types: list[str]) -> None:
    invalid = [e for e in event_types if e not in _VALID_EVENT_TYPES]
    if invalid:
        raise HTTPException(
            422,
            f"Invalid event_types: {invalid}. Use /api/os/events/catalog for canonical names.",
        )


async def _validate_source_pattern(source_pattern: str, db: AsyncSession) -> None:
    result = await db.execute(select(Connector).where(Connector.enabled == True))  # noqa: E712
    connectors = list(result.scalars().all())
    allowed_items = build_source_pattern_items(connectors, enabled_only=True, include_internal=True)
    allowed_patterns = [i.source_pattern for i in allowed_items]
    if any(fnmatch(source_pattern, p) for p in allowed_patterns):
        return
    raise HTTPException(
        422,
        f"Invalid source_pattern: {source_pattern}. Allowed namespaces: {allowed_patterns}",
    )


async def _validate_event_types_for_source(
    source_pattern: str,
    event_types: list[str],
    db: AsyncSession,
) -> None:
    result = await db.execute(select(Connector).where(Connector.enabled == True))  # noqa: E712
    connectors = list(result.scalars().all())
    items = build_source_pattern_items(connectors, enabled_only=True, include_internal=True)
    matched = next((i for i in items if fnmatch(source_pattern, i.source_pattern)), None)
    if not matched:
        return
    invalid = [e for e in event_types if e not in (matched.event_types or [])]
    if invalid:
        raise HTTPException(
            422,
            (
                f"Invalid event_types for source_pattern {source_pattern}: {invalid}. "
                f"Allowed event_types: {matched.event_types}"
            ),
        )


@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def list_all_subscriptions(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Subscription).order_by(Subscription.created_at))
    return result.scalars().all()


# ── Per-agent CRUD ──

async def _ensure_agent(agent_id: str, db: AsyncSession) -> Agent:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


@router.get("/agents/{agent_id}/subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions(agent_id: str, db: AsyncSession = Depends(get_db)):
    await _ensure_agent(agent_id, db)
    result = await db.execute(
        select(Subscription)
        .where(Subscription.agent_id == agent_id)
        .order_by(Subscription.created_at)
    )
    return result.scalars().all()


@router.post("/agents/{agent_id}/subscriptions", response_model=SubscriptionOut, status_code=201)
async def create_subscription(
    agent_id: str,
    body: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_agent(agent_id, db)
    _validate_event_types(body.event_types)
    await _validate_source_pattern(body.source_pattern, db)
    await _validate_event_types_for_source(body.source_pattern, body.event_types, db)
    sub = Subscription(
        agent_id=agent_id,
        source_pattern=body.source_pattern,
        event_types=body.event_types,
        filter_rules=body.filter_rules,
        enabled=body.enabled,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


@router.put("/agents/{agent_id}/subscriptions/{sub_id}", response_model=SubscriptionOut)
async def update_subscription(
    agent_id: str,
    sub_id: str,
    body: SubscriptionUpdate,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_agent(agent_id, db)
    sub = await db.get(Subscription, sub_id)
    if not sub or sub.agent_id != agent_id:
        raise HTTPException(404, "Subscription not found")
    updates = body.model_dump(exclude_unset=True)
    source_pattern = updates.get("source_pattern", sub.source_pattern)
    if "event_types" in updates:
        _validate_event_types(updates["event_types"])
    if "source_pattern" in updates:
        await _validate_source_pattern(source_pattern, db)
    if "event_types" in updates or "source_pattern" in updates:
        await _validate_event_types_for_source(source_pattern, updates.get("event_types", sub.event_types), db)
    for field, value in updates.items():
        setattr(sub, field, value)
    await db.commit()
    await db.refresh(sub)
    return sub


@router.delete("/agents/{agent_id}/subscriptions/{sub_id}", status_code=204)
async def delete_subscription(
    agent_id: str,
    sub_id: str,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_agent(agent_id, db)
    sub = await db.get(Subscription, sub_id)
    if not sub or sub.agent_id != agent_id:
        raise HTTPException(404, "Subscription not found")
    await db.delete(sub)
    await db.commit()
