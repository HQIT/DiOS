"""将 McpServer 记录转为 DiAgent MultiServerMCPClient 标准 dict 配置。"""

from __future__ import annotations

from typing import Any


def _headers_from_env(env: dict[str, Any] | None) -> dict[str, str]:
    """通用约定：HEADER_* → HTTP 头；Authorization / X-Api-Key 原样透传。"""
    headers: dict[str, str] = {}
    for k, v in (env or {}).items():
        if v is None:
            continue
        sv = str(v)
        if k.startswith("HEADER_") and len(k) > 7:
            headers[k[7:]] = sv
        elif k.lower() in ("authorization", "x-api-key"):
            headers[k] = sv
    return headers


def build_diagent_mcp_servers(servers: list[Any]) -> dict[str, dict[str, Any]]:
    """生成 DiAgent 可读的 mcp_servers.json 根对象（按 server name 索引）。"""
    out: dict[str, dict[str, Any]] = {}
    for s in servers:
        name = getattr(s, "name", None) or "unnamed"
        transport = (getattr(s, "transport", None) or "stdio").strip().lower()
        env = getattr(s, "env", None) or {}
        url = (getattr(s, "url", None) or "").strip()
        command = (getattr(s, "command", None) or "").strip()

        if transport in ("sse", "streamable-http", "http"):
            # DiAgent README: transport = http | sse | ...
            diagent_transport = "sse" if transport == "sse" else "http"
            endpoint = url or command
            if not endpoint:
                continue
            entry: dict[str, Any] = {
                "transport": diagent_transport,
                "url": endpoint,
            }
            headers = _headers_from_env(env)
            if headers:
                entry["headers"] = headers
            out[name] = entry
        else:
            if not command:
                continue
            out[name] = {
                "transport": "stdio",
                "command": command,
                "args": list(getattr(s, "args", None) or []),
                "env": dict(env),
            }
    return out
