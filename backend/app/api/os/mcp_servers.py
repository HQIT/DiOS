"""MCP Server CRUD：供 Agent 选用，下发任务时生成 mcp_servers.json。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.tables import McpServer
from app.models.schemas import McpServerCreate, McpServerUpdate, McpServerOut
from app.services.mcp_config import server_to_connection

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


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
