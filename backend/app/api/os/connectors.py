"""Connector CRUD：事件源（GitHub/GitLab/IMAP 等）的启用与配置。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors import registry
from app.db.database import get_db
from app.models.tables import Connector
from app.models.schemas import ConnectorCreate, ConnectorUpdate, ConnectorOut, ConnectorTypeOut
from app.services.connector_capabilities import build_source_pattern_items

router = APIRouter(prefix="/connectors", tags=["connectors"])

def _manifest_or_400(connector_type: str, *, canonical_only: bool = False):
    manifest = registry.manifest_for(connector_type)
    allowed = registry.instantiable_types()
    if (
        manifest is None
        or not manifest.instantiable
        or (canonical_only and connector_type != manifest.type)
    ):
        raise HTTPException(400, f"type must be one of {allowed}")
    return manifest


def _validate_config_or_400(manifest, config: dict, *, connector_type: str) -> None:
    try:
        registry.validate_config(manifest, config, connector_type=connector_type)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid {manifest.type} configuration: {exc}") from exc


@router.get("", response_model=list[ConnectorOut])
async def list_connectors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Connector).order_by(Connector.type, Connector.created_at))
    return result.scalars().all()


@router.get("/source-patterns", response_model=list[dict])
async def list_connector_source_patterns(db: AsyncSession = Depends(get_db)):
    """返回当前可用于订阅的 source pattern（由 Connector 统一定义）。"""
    result = await db.execute(select(Connector).order_by(Connector.type, Connector.created_at))
    connectors = list(result.scalars().all())
    items = build_source_pattern_items(connectors, enabled_only=True, include_internal=True)
    return [
        {
            "source_pattern": i.source_pattern,
            "label": i.label,
            "event_types": i.event_types,
            "connector_id": i.connector_id,
            "connector_name": i.connector_name,
            "connector_type": i.connector_type,
            "kind": i.kind,
        }
        for i in items
    ]


@router.get("/types", response_model=list[ConnectorTypeOut])
async def list_connector_types():
    """返回注册表中可创建的 Connector 类型及其配置契约。"""
    return list(registry.instantiable_manifests())


@router.post("", response_model=ConnectorOut, status_code=201)
async def create_connector(body: ConnectorCreate, db: AsyncSession = Depends(get_db)):
    manifest = _manifest_or_400(body.type, canonical_only=True)
    _validate_config_or_400(manifest, body.config or {}, connector_type=body.type)
    conn = Connector(type=body.type, name=body.name, enabled=body.enabled, config=body.config)
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


@router.get("/{connector_id}", response_model=ConnectorOut)
async def get_connector(connector_id: str, db: AsyncSession = Depends(get_db)):
    conn = await db.get(Connector, connector_id)
    if not conn:
        raise HTTPException(404, "Connector not found")
    return conn


@router.put("/{connector_id}", response_model=ConnectorOut)
async def update_connector(
    connector_id: str,
    body: ConnectorUpdate,
    db: AsyncSession = Depends(get_db),
):
    conn = await db.get(Connector, connector_id)
    if not conn:
        raise HTTPException(404, "Connector not found")
    updates = body.model_dump(exclude_unset=True)
    new_type = updates.get("type", conn.type)
    manifest = _manifest_or_400(new_type)
    config = updates.get("config", conn.config) or {}
    _validate_config_or_400(manifest, config, connector_type=new_type)
    for k, v in updates.items():
        setattr(conn, k, v)
    await db.commit()
    await db.refresh(conn)
    return conn


@router.delete("/{connector_id}", status_code=204)
async def delete_connector(connector_id: str, db: AsyncSession = Depends(get_db)):
    conn = await db.get(Connector, connector_id)
    if not conn:
        raise HTTPException(404, "Connector not found")
    await db.delete(conn)
    await db.commit()
