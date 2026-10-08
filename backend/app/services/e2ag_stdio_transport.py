"""Opt-in controlled stdio tool transport, owned by one application process.

Each grant owns a child and a serialized MCP session. Trusted administrator
configuration supplies the executable, arguments and environment; requests do
not. This is a tools-only subset, not arbitrary MCP server-initiated features.
No automatic tool replay or child restart follows a failed exchange.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess

from app.services.mcp_config import server_to_connection

MAX_MESSAGE = 1024 * 1024


class StdioExchangeError(Exception):
    pass


class StdioSession:
    def __init__(self, connection, *, timeout):
        self.connection = connection
        self.timeout = timeout
        self.process = None
        self.phase = "new"
        self.lock = asyncio.Lock()

    async def close(self):
        self.phase = "closed"
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 1)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    async def _start(self):
        command = self.connection["command"]
        arguments = self.connection.get("args", [])
        configured_env = self.connection.get("env", {})
        if not Path(command).is_absolute() or not Path(command).is_file():
            raise StdioExchangeError("STDIO_EXECUTABLE_NOT_ABSOLUTE_FILE")
        if not isinstance(arguments, list) or not all(isinstance(x, str) for x in arguments):
            raise StdioExchangeError("STDIO_INVALID_ARGUMENTS")
        if not isinstance(configured_env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in configured_env.items()):
            raise StdioExchangeError("STDIO_INVALID_ENVIRONMENT")
        # Do not implicitly pass application/model credentials to tool children.
        environment = {k: v for k, v in os.environ.items() if k.upper() in
                       {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG"}}
        environment.update(configured_env)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self.process = await asyncio.create_subprocess_exec(command, *arguments,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=environment, limit=MAX_MESSAGE + 1, **options)

    async def exchange(self, payload):
        async with self.lock:
            try:
                return await asyncio.wait_for(self._exchange(payload), self.timeout)
            except (OSError, ValueError, asyncio.TimeoutError, StdioExchangeError) as exc:
                await self.close()
                raise StdioExchangeError(type(exc).__name__) from exc
            except asyncio.CancelledError:
                await self.close()
                raise

    async def _exchange(self, payload):
        method = payload.get("method")
        if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
            raise StdioExchangeError("STDIO_INVALID_REQUEST")
        if method == "initialize":
            if self.phase != "new":
                raise StdioExchangeError("STDIO_INITIALIZATION_ORDER")
            params = payload.get("params")
            if not isinstance(params, dict) or params.get("protocolVersion") != "2025-06-18":
                raise StdioExchangeError("STDIO_PROTOCOL_VERSION_UNSUPPORTED")
            # Server-initiated sampling/roots/elicitation are not relayed.
            if params.get("capabilities") != {}:
                raise StdioExchangeError("STDIO_CLIENT_CAPABILITIES_UNSUPPORTED")
            await self._start()
        elif method == "notifications/initialized":
            if self.phase != "initialized" or "id" in payload:
                raise StdioExchangeError("STDIO_INITIALIZATION_ORDER")
        elif self.phase != "ready":
            raise StdioExchangeError("STDIO_SESSION_NOT_READY")
        request_id = payload.get("id")
        if method != "notifications/initialized" and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            raise StdioExchangeError("STDIO_INVALID_REQUEST_ID")
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_MESSAGE:
            raise StdioExchangeError("STDIO_MESSAGE_TOO_LARGE")
        if self.process is None or self.process.returncode is not None:
            raise StdioExchangeError("STDIO_CHILD_EXITED")
        self.process.stdin.write(raw + b"\n")
        await self.process.stdin.drain()
        if method == "notifications/initialized":
            self.phase = "ready"
            return 202, b""
        # Bounded notification processing; reject server-initiated requests.
        for _ in range(32):
            line = await self.process.stdout.readline()
            if not line or len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                raise StdioExchangeError("STDIO_INVALID_FRAME")
            response = json.loads(line.decode("utf-8"))
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise StdioExchangeError("STDIO_INVALID_RESPONSE")
            if "method" in response:
                if "id" in response or not str(response["method"]).startswith("notifications/"):
                    raise StdioExchangeError("STDIO_SERVER_REQUEST_UNSUPPORTED")
                continue
            if type(response.get("id")) is not type(request_id) or response.get("id") != request_id:
                raise StdioExchangeError("STDIO_RESPONSE_ID_MISMATCH")
            if ("result" in response) == ("error" in response):
                raise StdioExchangeError("STDIO_INVALID_RESPONSE")
            if method == "initialize":
                result = response.get("result")
                if not isinstance(result, dict) or result.get("protocolVersion") != "2025-06-18":
                    raise StdioExchangeError("STDIO_PROTOCOL_NEGOTIATION_FAILED")
                self.phase = "initialized"
            return 200, line.rstrip(b"\r\n")
        raise StdioExchangeError("STDIO_NOTIFICATION_LIMIT")


class StdioPool:
    def __init__(self, *, timeout=30, max_sessions=64):
        self.timeout, self.max_sessions = timeout, max_sessions
        self.sessions = {}

    async def exchange(self, grant_id, server, payload):
        connection = server_to_connection(server)
        if connection["transport"] != "stdio":
            raise StdioExchangeError("STDIO_TRANSPORT_MISMATCH")
        session = self.sessions.get(grant_id)
        if session is None:
            if payload.get("method") != "initialize" or len(self.sessions) >= self.max_sessions:
                raise StdioExchangeError("STDIO_SESSION_UNAVAILABLE")
            session = StdioSession(connection, timeout=self.timeout)
            self.sessions[grant_id] = session
        elif session.connection != connection:
            raise StdioExchangeError("STDIO_CONFIGURATION_CHANGED")
        return await session.exchange(payload)

    async def close_session(self, grant_id):
        session = self.sessions.get(grant_id)
        if session is not None:
            async with session.lock:
                await session.close()
            self.sessions.pop(grant_id, None)
        # A missing session accepts only an explicit initialize, never a tool
        # retry. Every request still has to pass the grant checks in the PEP.

    async def close(self):
        for grant_id in list(self.sessions):
            await self.close_session(grant_id)


@asynccontextmanager
async def stdio_transport_lifespan(app):
    pool = StdioPool()
    app.state.e2ag_stdio_pool = pool
    try:
        yield
    finally:
        await pool.close()
        del app.state.e2ag_stdio_pool
