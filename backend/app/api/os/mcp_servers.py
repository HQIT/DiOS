"""管理 MCP 服务注册与工具发现，凭据留在 DiOS。"""

import logging
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.models.tables import McpServer
from app.models.schemas import McpServerCreate, McpServerUpdate, McpServerOut
from app.services.mcp_config import server_to_connection
from app.services.mcp_tool_catalog import McpCatalogError, McpToolSummary, list_remote_mcp_tools

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpToolCatalog:
    """用于工具选择的服务名称和目录，不包含连接凭据。"""

    server_name: str
    tools: list[McpToolSummary]


def _normalize_transport(t: str) -> str:
    t = (t or "stdio").strip().lower()
    if t in ("http", "streamable-http", "streamablehttp"):
        return "streamable_http"
    return t


def _validate_body(transport: str, url: str, command: str) -> None:
    transport = _normalize_transport(transport)
    if transport in ("streamable_http", "sse", "websocket"):
        if not (url or "").strip():
            raise HTTPException(400, f"transport={transport} requires url")
    elif transport == "stdio":
        if not (command or "").strip():
            raise HTTPException(400, "transport=stdio requires command")
    else:
        raise HTTPException(400, f"unsupported transport: {transport}")


@router.get("", response_model=list[McpServerOut])
async def list_mcp_servers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(McpServer).order_by(McpServer.name))
    return result.scalars().all()


@router.post("", response_model=McpServerOut, status_code=201)
async def create_mcp_server(body: McpServerCreate, db: AsyncSession = Depends(get_db)):
    transport = _normalize_transport(body.transport)
    _validate_body(transport, body.url, body.command)
    srv = McpServer(
        name=body.name,
        transport=transport,
        url=body.url or "",
        headers=body.headers or {},
        command=body.command or "",
        args=body.args or [],
        env=body.env or {},
    )
    # 提前校验可下发
    try:
        server_to_connection(srv)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add(srv)
    await db.commit()
    await db.refresh(srv)
    return srv


@router.get("/{server_id}", response_model=McpServerOut)
async def get_mcp_server(server_id: str, db: AsyncSession = Depends(get_db)):
    srv = await db.get(McpServer, server_id)
    if not srv:
        raise HTTPException(404, "MCP server not found")
    return srv


@router.get("/{server_id}/tools")
async def get_mcp_tools(server_id: str, db: AsyncSession = Depends(get_db)) -> McpToolCatalog:
    """从已注册的 Streamable HTTP 服务读取工具列表。"""
    if not settings.access_token.strip():
        raise HTTPException(503, "读取 MCP 工具目录需要配置 DiOS Access Token")
    server = await db.get(McpServer, server_id)
    if server is None:
        raise HTTPException(404, "MCP server not found")
    if _normalize_transport(server.transport) != "streamable_http":
        raise HTTPException(422, "Tool discovery requires a Streamable HTTP MCP server")
    try:
        async with httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
            tools = await list_remote_mcp_tools(client, server.url, server.headers or {})
    except (McpCatalogError, ValueError) as error:
        logger.warning("MCP tool discovery failed (server_id=%s, error=%s)", server_id, type(error).__name__)
        raise HTTPException(502, "无法读取 MCP 工具目录，请检查服务地址和凭据") from error
    return McpToolCatalog(server.name, tools)


@router.put("/{server_id}", response_model=McpServerOut)
async def update_mcp_server(
    server_id: str,
    body: McpServerUpdate,
    db: AsyncSession = Depends(get_db),
):
    srv = await db.get(McpServer, server_id)
    if not srv:
        raise HTTPException(404, "MCP server not found")
    updates = body.model_dump(exclude_unset=True)
    if "transport" in updates and updates["transport"] is not None:
        updates["transport"] = _normalize_transport(updates["transport"])
    for k, v in updates.items():
        setattr(srv, k, v)
    _validate_body(srv.transport, srv.url, srv.command)
    try:
        server_to_connection(srv)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    await db.refresh(srv)
    return srv


@router.delete("/{server_id}", status_code=204)
async def delete_mcp_server(server_id: str, db: AsyncSession = Depends(get_db)):
    srv = await db.get(McpServer, server_id)
    if not srv:
        raise HTTPException(404, "MCP server not found")
    await db.delete(srv)
    await db.commit()
