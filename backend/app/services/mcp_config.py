"""将 DiOS McpServer 记录转为 DiAgent / langchain-mcp-adapters 期望的配置对象。"""

from __future__ import annotations

from typing import Any


def server_to_connection(srv: Any) -> dict[str, Any]:
    """单条 McpServer → connection dict（不含 name）。"""
    transport = (getattr(srv, "transport", None) or "stdio").strip().lower()
    # 归一别名
    if transport in ("streamable-http", "streamablehttp", "http"):
        transport = "streamable_http"

    if transport in ("streamable_http", "sse", "websocket"):
        url = (getattr(srv, "url", None) or "").strip()
        if not url:
            raise ValueError(f"MCP server {getattr(srv, 'name', '?')}: remote transport requires url")
        conn: dict[str, Any] = {"transport": transport, "url": url}
        headers = getattr(srv, "headers", None) or {}
        if headers:
            conn["headers"] = dict(headers)
        return conn

    # stdio
    command = (getattr(srv, "command", None) or "").strip()
    if not command:
        raise ValueError(f"MCP server {getattr(srv, 'name', '?')}: stdio transport requires command")
    conn = {
        "transport": "stdio",
        "command": command,
        "args": list(getattr(srv, "args", None) or []),
    }
    env = getattr(srv, "env", None) or {}
    if env:
        conn["env"] = dict(env)
    return conn


def servers_to_mcp_config(servers: list[Any]) -> dict[str, Any]:
    """多条 → { name: connection }。"""
    out: dict[str, Any] = {}
    for srv in servers:
        name = getattr(srv, "name", None) or getattr(srv, "id", "unnamed")
        out[str(name)] = server_to_connection(srv)
    return out
