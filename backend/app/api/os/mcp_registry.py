"""MCP Registry 代理：规范化 Official MCP Registry 元数据，避免前端 CORS。"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Query

router = APIRouter(prefix="/mcp-registry", tags=["mcp-registry"])
logger = logging.getLogger(__name__)

_REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"
_SEARCH_CACHE: dict[str, list[dict[str, Any]]] = {}


def _normalize_transport(value: str) -> str:
    value = (value or "").strip().lower()
    if value in {"streamable-http", "streamablehttp", "http"}:
        return "streamable_http"
    return value


def _simplify(server_raw: dict) -> dict[str, Any]:
    """将 Registry server.json 压缩为 Console 注册表卡片所需字段。"""
    server = server_raw.get("server", server_raw)
    official_meta = (server_raw.get("_meta") or {}).get(
        "io.modelcontextprotocol.registry/official", {}
    )

    command = ""
    args: list[str] = []
    env_hints: dict[str, str] = {}
    package_registry = ""
    package_identifier = ""
    package_transport = ""

    for package in server.get("packages") or []:
        registry_type = package.get("registryType", "")
        identifier = package.get("identifier", "")
        transport = _normalize_transport((package.get("transport") or {}).get("type", ""))
        if registry_type == "npm":
            command, args = "npx", ["-y", identifier]
        elif registry_type == "oci":
            command, args = "docker", ["run", "-i", "--rm", identifier]
        elif registry_type == "pip":
            command, args = "uvx", [identifier]
        else:
            continue

        package_registry = registry_type
        package_identifier = identifier
        package_transport = transport or "stdio"
        for variable in package.get("environmentVariables") or []:
            name = variable.get("name", "")
            if name:
                env_hints[name] = variable.get("description", "")
        break

    remote_url = ""
    remote_transport = ""
    header_hints: dict[str, str] = {}
    for remote in server.get("remotes") or []:
        if not remote.get("url"):
            continue
        remote_url = remote["url"]
        remote_transport = _normalize_transport(remote.get("type", "")) or "streamable_http"
        for header in remote.get("headers") or []:
            name = header.get("name", "")
            if name:
                description = header.get("description", "")
                if header.get("isRequired"):
                    description = f"{description}（必填）" if description else "必填"
                header_hints[name] = description
        break

    repository = server.get("repository") or {}
    return {
        "name": server.get("name", ""),
        "title": server.get("title", ""),
        "description": server.get("description", ""),
        "version": server.get("version", ""),
        "command": command,
        "args": args,
        "env_hints": env_hints,
        "transport": remote_transport or package_transport,
        "url": remote_url,
        "header_hints": header_hints,
        "registry_type": package_registry,
        "package": package_identifier,
        "repository_url": repository.get("url", "") if isinstance(repository, dict) else "",
        "status": official_meta.get("status", ""),
        "is_latest": bool(official_meta.get("isLatest")),
        "published_at": official_meta.get("publishedAt", ""),
    }


def _dedupe_latest(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个名称仅保留 Registry 标记的 latest；旧 API 数据则退化为最后发布版本。"""
    by_name: dict[str, dict[str, Any]] = {}
    for server in servers:
        name = server.get("name", "")
        if not name:
            continue
        current = by_name.get(name)
        if current is None:
            by_name[name] = server
            continue
        if server.get("is_latest") or (
            not current.get("is_latest")
            and server.get("published_at", "") >= current.get("published_at", "")
        ):
            by_name[name] = server
    return list(by_name.values())


@router.get("/search")
async def search_registry(
    q: str = Query("", description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100),
):
    query = q.strip().lower()
    cache_key = query or "__all__"
    if cache_key not in _SEARCH_CACHE:
        try:
            _SEARCH_CACHE[cache_key] = await _fetch_query(query, max(limit, 20))
        except Exception as error:
            logger.warning("Failed to fetch MCP registry: %s", error)
            return {"servers": [], "total": 0, "source": "official", "status": "unavailable"}

    matched = _SEARCH_CACHE[cache_key]
    return {
        "servers": matched[:limit],
        "total": len(matched),
        "source": "official",
        "status": "ok",
    }


async def _fetch_query(query: str, limit: int) -> list[dict[str, Any]]:
    """使用 Registry 原生 search 参数按需查询，避免首次搜索扫描全库。"""
    all_servers: list[dict[str, Any]] = []
    cursor: str | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(3):
            params: dict[str, Any] = {"limit": min(limit, 100)}
            if query:
                params["search"] = query
            if cursor:
                params["cursor"] = cursor
            response = await client.get(_REGISTRY_URL, params=params)
            response.raise_for_status()
            data = response.json()
            all_servers.extend(
                simplified
                for raw in data.get("servers", [])
                if (simplified := _simplify(raw))["name"]
            )
            cursor = data.get("metadata", {}).get("nextCursor")
            if not cursor or len(all_servers) >= limit:
                break

    result = _dedupe_latest(all_servers)[:limit]
    logger.info("MCP Registry query loaded: q=%r servers=%d", query, len(result))
    return result
