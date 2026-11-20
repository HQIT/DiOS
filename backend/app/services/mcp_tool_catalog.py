"""读取已注册 MCP 服务的工具元数据，供管理员配置工具名单。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)
_PROTOCOL_VERSION = "2025-06-18"
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_PAGES = 10
_MAX_TOOLS = 500
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


class McpCatalogError(Exception):
    """上游未返回可用于界面展示的工具目录。"""


@dataclass(frozen=True)
class McpToolSummary:
    """可显示的工具元数据，name 用于保存授权名单。"""

    name: str
    title: str
    description: str


async def _rpc_result(response: httpx.Response, request_id: int) -> dict:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/json", "text/event-stream"}:
        raise McpCatalogError("Unexpected MCP response content type")

    buffer = bytearray()
    frame = []
    bytes_received = 0
    async for chunk in response.aiter_bytes():
        bytes_received += len(chunk)
        if bytes_received > _MAX_RESPONSE_BYTES:
            raise McpCatalogError("MCP catalog response is too large")
        buffer.extend(chunk)
        if content_type == "application/json":
            continue
        while (newline := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:newline]).rstrip(b"\r")
            del buffer[:newline + 1]
            if line.startswith(b"data:"):
                frame.append(line[5:].removeprefix(b" "))
            elif not line and frame:
                envelope = json.loads(b"\n".join(frame))
                frame.clear()
                if isinstance(envelope, dict) and envelope.get("id") == request_id:
                    return _result_object(envelope, request_id)
    if content_type == "application/json":
        return _result_object(json.loads(buffer), request_id)
    raise McpCatalogError("MCP stream ended before returning a catalog response")


def _result_object(envelope: object, request_id: int) -> dict:
    if isinstance(envelope, dict) and envelope.get("id") == request_id and isinstance(envelope.get("result"), dict):
        return envelope["result"]
    raise McpCatalogError("MCP server did not return a result for the request")


def _tool_summary(tool: object) -> McpToolSummary | None:
    if not isinstance(tool, dict):
        return None
    name = tool.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        return None
    title, description = tool.get("title"), tool.get("description")
    return McpToolSummary(name, title[:160] if isinstance(title, str) else "",
                          description[:500] if isinstance(description, str) else "")


async def _list_pages(client: httpx.AsyncClient, url: str, headers: httpx.Headers) -> list[McpToolSummary]:
    tools = {}
    seen_cursors = set()
    params = {}
    for request_id in range(2, 2 + _MAX_PAGES):
        payload = {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": params}
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            page = await _rpc_result(response, request_id)
        if not isinstance(page.get("tools"), list):
            raise McpCatalogError("MCP catalog did not return a tools array")
        for tool in page["tools"]:
            summary = _tool_summary(tool)
            if summary is not None:
                tools[summary.name] = summary
                if len(tools) > _MAX_TOOLS:
                    raise McpCatalogError("MCP catalog exceeded the tool limit")
        cursor = page.get("nextCursor")
        if cursor is None:
            return list(tools.values())
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise McpCatalogError("MCP catalog returned an invalid cursor")
        seen_cursors.add(cursor)
        params = {"cursor": cursor}
    raise McpCatalogError("MCP catalog exceeded the page limit")


async def list_remote_mcp_tools(
    client: httpx.AsyncClient, url: str, registered_headers: dict[str, str],
) -> list[McpToolSummary]:
    """建立临时 MCP 会话并列出工具。网络或协议失败时抛出 McpCatalogError。"""
    headers = httpx.Headers(registered_headers)
    headers.pop("mcp-session-id", None)
    headers.pop("mcp-protocol-version", None)
    headers["accept"] = "application/json, text/event-stream"
    headers["content-type"] = "application/json"
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "DiOS", "version": "0.1"},
    }}
    try:
        async with asyncio.timeout(12):
            async with client.stream("POST", url, headers=headers, json=initialize) as response:
                result = await _rpc_result(response, 1)
                session = response.headers.get("mcp-session-id")
            if session:
                if not re.fullmatch(r"[!-~]{1,256}", session):
                    raise McpCatalogError("MCP server returned an invalid session ID")
                headers["mcp-session-id"] = session
            try:
                version = result.get("protocolVersion")
                if not isinstance(version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
                    raise McpCatalogError("MCP server returned an invalid protocol version")
                headers["mcp-protocol-version"] = version
                notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
                async with client.stream("POST", url, headers=headers, json=notification) as response:
                    response.raise_for_status()
                return await _list_pages(client, url, headers)
            finally:
                if session:
                    await _close_session(client, url, headers)
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError) as error:
        raise McpCatalogError("MCP tool discovery failed") from error


async def _close_session(client: httpx.AsyncClient, url: str, headers: httpx.Headers) -> None:
    try:
        async with client.stream("DELETE", url, headers=headers, timeout=1) as response:
            if response.status_code != 405:
                response.raise_for_status()
    except httpx.HTTPError as error:
        logger.warning("MCP catalog session could not be closed (%s)", type(error).__name__)
