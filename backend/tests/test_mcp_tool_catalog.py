"""验证读取工具目录遵循 Streamable HTTP 会话约定。"""

import json
import unittest

import httpx

from app.services.mcp_tool_catalog import list_remote_mcp_tools


class McpToolCatalogTest(unittest.IsolatedAsyncioTestCase):
    async def test_lists_metadata_from_paginated_stateful_server(self):
        requests = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                requests.append(("close", request.headers["mcp-session-id"]))
                return httpx.Response(204)
            payload = json.loads(request.content)
            method = payload["method"]
            requests.append((method, request.headers.get("mcp-session-id")))
            if method == "initialize":
                return httpx.Response(200, headers={"Mcp-Session-Id": "demo"}, json={
                    "jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "2025-06-18"},
                })
            if method == "notifications/initialized":
                return httpx.Response(202)
            result = {"tools": [{"name": "fetch", "description": "Read a webpage"}]}
            if not payload["params"]:
                result = {"tools": [{"name": "search", "title": "Web search"}], "nextCursor": "next"}
            envelope = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=f"data: {json.dumps(envelope)}\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            tools = await list_remote_mcp_tools(client, "http://catalog.test/mcp", {})

        self.assertEqual([tool.name for tool in tools], ["search", "fetch"])
        self.assertEqual([tool.title for tool in tools], ["Web search", ""])
        self.assertEqual(requests, [
            ("initialize", None), ("notifications/initialized", "demo"),
            ("tools/list", "demo"), ("tools/list", "demo"), ("close", "demo"),
        ])
