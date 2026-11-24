"""Governed ACP v1 stdio adapter for the Worker JSONL bridge.

The registered command/configuration is operator-owned. Prompt input cannot select
commands, credentials, directories or MCP endpoints. Native tool governance remains
an explicit #34 qualification gate; client operations use the Worker tool gate.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
from uuid import uuid4
from acp.connection import Connection
from acp.exceptions import RequestError
from .acp_transport import BoundedTransport, request_id
from .protocol import WorkloadError as RuntimeErrorCode
from .protocol import encoded


def emit(value):
    sys.stdout.buffer.write(encoded(value) + b"\n")
    sys.stdout.buffer.flush()


def diagnostic(code):
    # Provider stderr/errors may include credentials; only normalized codes leave this bridge.
    sys.stderr.write(
        json.dumps(
            {
                "code": code,
                "run_id": os.environ.get("DIOS_RUN_ID"),
                "attempt_id": os.environ.get("DIOS_ATTEMPT_ID"),
            }
        )
        + "\n"
    )
    sys.stderr.flush()


class AcpClient:
    def __init__(
        self,
        process,
        *,
        emit_event=emit,
        limit=32000,
        tool_client=None,
        client_read_tool=None,
        permission_decoder=None,
    ):
        self.process = process
        self.emit = emit_event
        self.limit = limit
        self.pending = set()
        self.prompt_task = None
        self.session_id = None
        self.session_close_supported = False
        self.output = ""
        self.handlers = set()
        self.cancelling = False
        self.cancel_acknowledged = False
        self.transport = BoundedTransport(self)
        self.connection = Connection(self.handle, self.transport, listening=False)
        self.permission_decoder = permission_decoder
        self.tool_client = tool_client
        self.client_read_tool = client_read_tool
        self.tool_activity = {}
        self.native_tool_seen = False
        self.denials = 0
        self.reader = None
        self.stderr = None
        self.failure = None

    async def send(self, value):
        await self.transport.send(value)

    async def read(self):
        await self.connection.main_loop()

    async def handle(self, method, params, is_notification):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            if self.cancelling:
                raise asyncio.CancelledError
            return await self.request(method, params or {}, request_id.get())
        except asyncio.CancelledError:
            if method == "session/request_permission":
                return {"outcome": {"outcome": "cancelled"}}
            raise RequestError.request_cancelled() from None
        except RequestError:
            raise
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, RuntimeErrorCode)
                else "TOOL_CHANNEL_UNAVAILABLE"
            )
            raise RequestError(-32000, code) from None
        finally:
            self.handlers.discard(task)

    async def discard_stderr(self):
        # Drain concurrently so a verbose child cannot deadlock RPC. Never forward raw credentials.
        while await self.process.stderr.read(8192):
            pass

    async def incoming(self, message):
        method = message["method"]
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise RuntimeErrorCode("ACP_FRAME_INVALID")
        if method == "session/update":
            if self.session_id is None or params.get("sessionId") != self.session_id:
                raise RuntimeErrorCode("SESSION_CHANGED")
            update = params.get("update", {})
            if not isinstance(update, dict):
                raise RuntimeErrorCode("ACP_FRAME_INVALID")
            if update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
                self.native_tool_seen = True
                if self.tool_client:
                    call_id = update.get("toolCallId")
                    if (
                        not isinstance(call_id, str)
                        or not call_id
                        or len(call_id) > 160
                    ):
                        raise RuntimeErrorCode("ACP_TOOL_ACTIVITY_UNVERIFIED")
                    if (
                        call_id not in self.tool_activity
                        and len(self.tool_activity) >= 128
                    ):
                        raise RuntimeErrorCode("TOOL_CALL_LIMIT_EXCEEDED")
                    record = self.tool_activity.setdefault(call_id, {})
                    for field in ("name", "title", "rawInput"):
                        if field in update and update[field] is not None:
                            if (
                                field in record
                                and record[field] != update[field]
                                and (
                                    record.get("status") in {"completed", "failed"}
                                    or update.get("sessionUpdate") == "tool_call"
                                )
                            ):
                                raise RuntimeErrorCode("ACP_TOOL_ACTIVITY_CHANGED")
                            record[field] = update[field]
                    if update.get("status") is not None:
                        if (
                            record.get("status") in {"completed", "failed"}
                            and record["status"] != update["status"]
                        ):
                            raise RuntimeErrorCode("ACP_TOOL_ACTIVITY_CHANGED")
                        record["status"] = update["status"]
                else:
                    diagnostic("ACP_TOOL_ACTIVITY_UNVERIFIED")
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    text = content.get("text")
                    if not isinstance(text, str):
                        raise RuntimeErrorCode("ACP_FRAME_INVALID")
                    if len((self.output + text).encode()) > self.limit:
                        raise RuntimeErrorCode("OUTPUT_LIMIT_EXCEEDED")
                    self.output += text
                    for start in range(0, len(text), 2000):
                        self.emit(
                            {"type": "output", "text": text[start : start + 2000]}
                        )
        if "id" in message:
            token = request_id.set(message["id"])
            try:
                result = await self.handle(method, params, False)
                await self.send(
                    {"jsonrpc": "2.0", "id": message["id"], "result": result}
                )
            except RequestError as exc:
                await self.send(
                    {"jsonrpc": "2.0", "id": message["id"], "error": exc.to_error_obj()}
                )
            finally:
                request_id.reset(token)

    async def request(self, method, params, key):
        if method == "fs/read_text_file" and self.tool_client and self.client_read_tool:
            if params.get("sessionId") != self.session_id:
                raise RuntimeErrorCode("SESSION_CHANGED")
            if set(params) - {"sessionId", "path", "line", "limit"}:
                raise RuntimeErrorCode("ACP_FILE_REQUEST_INVALID")
            path = params.get("path")
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or len(path) > 4096
            ):
                raise RuntimeErrorCode("ACP_FILE_REQUEST_INVALID")
            args = {"path": path}
            for field in ("line", "limit"):
                if field in params:
                    if (
                        type(params[field]) is not int
                        or not 1 <= params[field] <= 100000
                    ):
                        raise RuntimeErrorCode("ACP_FILE_REQUEST_INVALID")
                    args[field] = params[field]
            invocation_key = (
                "acp-read:"
                + hashlib.sha256(
                    encoded({"session": self.session_id, "id": key})
                ).hexdigest()
            )
            value = await self.tool_client.call(
                self.client_read_tool, args, invocation_key
            )
            content = value.get("content", [])
            if (
                value.get("isError")
                or not content
                or any(
                    not isinstance(c, dict)
                    or c.get("type") != "text"
                    or not isinstance(c.get("text"), str)
                    for c in content
                )
            ):
                raise RuntimeErrorCode("ACP_FILE_RESULT_INVALID")
            text = "".join(c["text"] for c in content)
            if len(text.encode()) > 65536:
                raise RuntimeErrorCode("ACP_FILE_RESULT_TOO_LARGE")
            return {"content": text}
        if method == "session/request_permission":
            if params.get("sessionId") != self.session_id:
                raise RuntimeErrorCode("SESSION_CHANGED")
            options = params.get("options", [])
            if not isinstance(options, list):
                raise RuntimeErrorCode("ACP_FRAME_INVALID")
            if self.tool_client and self.permission_decoder:
                try:
                    permission_call = params.get("toolCall", {})
                    if not isinstance(permission_call, dict):
                        raise RuntimeErrorCode("ACP_FRAME_INVALID")
                    prior = self.tool_activity.get(
                        permission_call.get("toolCallId"), {}
                    )
                    mapped = self.permission_decoder(
                        {
                            **prior,
                            **{
                                k: v
                                for k, v in permission_call.items()
                                if v is not None
                            },
                        }
                    )
                    permit = await self.tool_client.request("permit_proxy", mapped)
                    allow = next(
                        (
                            x
                            for x in options
                            if isinstance(x, dict) and x.get("kind") == "allow_once"
                        ),
                        None,
                    )
                    if (
                        permit.get("forward_to_gate") is True
                        and allow
                        and isinstance(allow.get("optionId"), str)
                    ):
                        return {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": allow["optionId"],
                            }
                        }
                except (RuntimeErrorCode, KeyError, TypeError):
                    pass
            reject = next(
                (
                    x
                    for x in options
                    if isinstance(x, dict) and x.get("kind") == "reject_once"
                ),
                None,
            )
            outcome = (
                {"outcome": "selected", "optionId": reject["optionId"]}
                if reject
                else {"outcome": "cancelled"}
            )
            self.denials += 1
            diagnostic("TOOL_PERMISSION_DENIED")
            if self.tool_client:
                await self.tool_client.request(
                    "deny_native", {"method": "session/request_permission"}
                )
            return {"outcome": outcome}
        if self.tool_client:
            normalized = (
                "terminal"
                if method.startswith("terminal/")
                else "fs/write_text_file"
                if method == "fs/write_text_file"
                else "unsupported"
            )
            await self.tool_client.request("deny_native", {"method": normalized})
        raise RequestError(-32601, "Client operation is not enabled")

    async def _request(self, method, params):
        try:
            result = await self.connection.send_request(method, params)
        except RequestError:
            diagnostic("ACP_REMOTE_ERROR")
            raise RuntimeErrorCode(
                "ACP_AUTH_FAILED" if method == "authenticate" else "ACP_REQUEST_FAILED"
            ) from None
        except (ConnectionError, OSError):
            raise self.failure or RuntimeErrorCode("ACP_TRANSPORT_FAILED") from None
        if not isinstance(result, dict):
            raise RuntimeErrorCode("ACP_FRAME_INVALID")
        return result

    async def call(self, method, params, timeout=30):
        if self.failure:
            raise self.failure
        if self.cancelling:
            raise RuntimeErrorCode("ACP_CANCELLED")
        if (
            method == "session/prompt"
            and self.prompt_task
            and not self.prompt_task.done()
        ):
            raise RuntimeErrorCode("ACP_PROMPT_ALREADY_RUNNING")
        task = asyncio.create_task(self._request(method, params))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        if method == "session/prompt":
            self.prompt_task = task
        try:
            # Keep the original prompt ID until session/cancel is acknowledged.
            return await asyncio.wait_for(
                asyncio.shield(task) if method == "session/prompt" else task, timeout
            )
        except TimeoutError:
            raise RuntimeErrorCode("ACP_QUERY_TIMEOUT") from None

    async def cancel(self, timeout=2):
        self.cancelling = True
        for task in tuple(self.handlers):
            task.cancel()
        if self.session_id and self.prompt_task and not self.prompt_task.done():
            try:
                async with asyncio.timeout(timeout):
                    await self.connection.send_notification(
                        "session/cancel", {"sessionId": self.session_id}
                    )
                    response = await asyncio.shield(self.prompt_task)
                self.cancel_acknowledged = response.get("stopReason") == "cancelled"
                diagnostic(
                    "ACP_CANCEL_ACKNOWLEDGED"
                    if self.cancel_acknowledged
                    else "ACP_CANCEL_COMPLETION_RACE"
                )
            except (RuntimeErrorCode, ConnectionError, OSError, TimeoutError):
                diagnostic("ACP_CANCEL_UNCONFIRMED")
        return self.cancel_acknowledged

    async def close_session(self, timeout=1):
        if not self.session_id or not self.session_close_supported:
            return
        try:
            await asyncio.wait_for(
                self._request("session/close", {"sessionId": self.session_id}),
                timeout,
            )
            diagnostic("ACP_SESSION_CLOSED")
        except (RuntimeErrorCode, ConnectionError, OSError, TimeoutError):
            # The process still has to exit before any result can be accepted.
            diagnostic("ACP_SESSION_CLOSE_UNCONFIRMED")

    async def close(self):
        async def drain_after_reader():
            if self.reader:
                await asyncio.shield(self.reader)
            # A rejected frame stops protocol parsing. Keep the pipe flowing
            # during teardown so Process.wait cannot deadlock on buffered output.
            while await self.process.stdout.read(8192):
                pass

        drain = asyncio.create_task(drain_after_reader())
        try:
            await self.cancel()
            await self.close_session()
            self.process.stdin.close()
            await asyncio.wait_for(self.process.wait(), 1)
        except (OSError, ConnectionError, TimeoutError):
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 0.5)
                except TimeoutError:
                    self.process.kill()
                    try:
                        await asyncio.wait_for(self.process.wait(), 0.5)
                    except TimeoutError:
                        raise RuntimeErrorCode("ACP_CHILD_EXIT_UNCONFIRMED") from None
        finally:
            await self.connection.close()
            for task in (drain, self.reader, self.stderr, *self.pending):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(
                    t
                    for t in (
                        drain,
                        self.reader,
                        self.stderr,
                        self.prompt_task,
                        *self.pending,
                    )
                    if t
                ),
                return_exceptions=True,
            )


def configuration(path):
    source = Path(path)
    if source.stat().st_size > 16384:
        raise RuntimeErrorCode("ACP_CONFIG_INVALID")
    config = json.loads(source.read_text(encoding="utf-8"))
    command = config.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(x, str) and x for x in command)
    ):
        raise RuntimeErrorCode("ACP_CONFIG_INVALID")
    if not Path(command[0]).is_absolute():
        raise RuntimeErrorCode("ACP_EXECUTABLE_MUST_BE_ABSOLUTE")
    cwd = Path(config.get("cwd", "/tmp")).resolve()
    if not cwd.is_dir():
        raise RuntimeErrorCode("ACP_WORKSPACE_MISSING")
    env = dict(os.environ)
    # A launcher adapter for preprovisioned files; enterprise Secret lifecycle is #33.
    for name, path in config.get("credential_env_files", {}).items():
        if not re.fullmatch("[A-Z][A-Z0-9_]{0,79}", name):
            raise RuntimeErrorCode("ACP_CONFIG_INVALID")
        secret = Path(path)
        if secret.stat().st_size > 16384:
            raise RuntimeErrorCode("ACP_CREDENTIAL_INVALID")
        env[name] = secret.read_text(encoding="utf-8").strip()
        if not env[name]:
            raise RuntimeErrorCode("ACP_CREDENTIAL_INVALID")
    return config, command, cwd, env


async def execute(
    path,
    prompt,
    decode_result,
    *,
    required_tools=(),
    activity_decoder=None,
    output_decoder=None,
    permission_decoder=None,
):
    """Run one ACP turn and map its result through an application-owned validator."""
    config, command, cwd, env = configuration(path)
    timeout = config.get("prompt_timeout", 240)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
        raise RuntimeErrorCode("ACP_CONFIG_INVALID")
    SOCKET_ENV = "DIOS_TOOL_SOCKET"
    from .tools import ToolChannelClient

    tool_client = ToolChannelClient(env[SOCKET_ENV]) if env.get(SOCKET_ENV) else None
    tool_description = await tool_client.request("describe", {}) if tool_client else {}
    read_tool = tool_description.get("client_read_tool")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=256 * 1024,
    )
    peer = AcpClient(
        process,
        tool_client=tool_client,
        client_read_tool=read_tool,
        permission_decoder=permission_decoder,
    )
    peer.reader = asyncio.create_task(peer.read())
    peer.stderr = asyncio.create_task(peer.discard_stderr())
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    interrupted = False

    def interrupt():
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, interrupt)
        except (NotImplementedError, RuntimeError):
            pass
    result = None
    try:
        initialized = await peer.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": (
                    {"fs": {"readTextFile": True, "writeTextFile": False}}
                    if read_tool
                    else {}
                ),
                "clientInfo": {"name": "dios-acp-bridge", "version": "0.1"},
            },
        )
        if initialized.get("protocolVersion") != 1:
            raise RuntimeErrorCode("ACP_VERSION_UNSUPPORTED")
        capabilities = initialized.get("agentCapabilities", {}).get(
            "sessionCapabilities", {}
        )
        peer.session_close_supported = isinstance(capabilities, dict) and isinstance(
            capabilities.get("close"), dict
        )
        method = config.get("authentication_method")
        if method:
            if not any(
                a.get("id") == method for a in initialized.get("authMethods", [])
            ):
                raise RuntimeErrorCode("ACP_AUTH_METHOD_UNSUPPORTED")
            await peer.call("authenticate", {"methodId": method})
        mcp_servers = []
        if tool_client:
            mcp_servers = [
                {
                    "name": "dios",
                    "command": sys.executable,
                    "args": [
                        "-m",
                        "dios_sdk.mcp",
                        "--socket",
                        env[SOCKET_ENV],
                        "--instance",
                        str(uuid4()),
                    ],
                    "env": [
                        {
                            "name": "PYTHONPATH",
                            "value": str(Path(__file__).resolve().parents[1]),
                        }
                    ],
                }
            ]
        session = await peer.call(
            "session/new", {"cwd": str(cwd), "mcpServers": mcp_servers}
        )
        sid = session.get("sessionId")
        if not isinstance(sid, str) or not 1 <= len(sid) <= 160:
            raise RuntimeErrorCode("SESSION_INVALID")
        peer.session_id = sid
        emit({"type": "session", "session_id": sid})
        if tool_client:
            await tool_client.ready(sid)
        mode = config.get("mode_id")
        if mode:
            modes = session.get("modes", {}).get("availableModes", [])
            if not any(m.get("id") == mode for m in modes):
                raise RuntimeErrorCode("ACP_MODE_UNSUPPORTED")
            await peer.call("session/set_mode", {"sessionId": sid, "modeId": mode})
        response = await peer.call(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
            timeout=timeout,
        )
        if response.get("stopReason") != "end_turn":
            raise RuntimeErrorCode("RESULT_INCOMPLETE")
        if peer.native_tool_seen:
            if tool_client is None:
                raise RuntimeErrorCode("ACP_TOOL_ACTIVITY_UNVERIFIED")
            activity = []
            for call in peer.tool_activity.values():
                if activity_decoder:
                    call = activity_decoder(call)
                if not isinstance(call.get("name"), str) or not isinstance(
                    call.get("rawInput"), dict
                ):
                    raise RuntimeErrorCode("ACP_TOOL_ACTIVITY_UNVERIFIED")
                activity.append({"name": call["name"], "arguments": call["rawInput"]})
            await tool_client.request("verify_activity", {"activity": activity})
        if required_tools:
            if tool_client is None:
                raise RuntimeErrorCode("EVIDENCE_INSUFFICIENT")
            await tool_client.request(
                "verify_required", {"requirements": list(required_tools)}
            )
        try:
            parsed = (output_decoder or json.loads)(peer.output)
        except ValueError:
            raise RuntimeErrorCode("RESULT_FORMAT_INVALID") from None
        result = decode_result(parsed)
        if len(encoded(result)) > 60000:
            raise RuntimeErrorCode("OUTPUT_LIMIT_EXCEEDED")
    except asyncio.CancelledError:
        diagnostic("ACP_CANCEL_REQUESTED")
        interrupted = True
    finally:
        cleanup = asyncio.create_task(peer.close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
            await asyncio.shield(cleanup)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass
    if interrupted:
        return 130
    # Completion follows protocol validation and child cleanup; partial outputs remain visible.
    emit({"type": "result", "session_id": sid, "result": result})
    # Some persistent ACP servers exit nonzero when stdin closes after end_turn.
    # Retain the validated result; the runtime's explicit acceptance policy decides.
    if process.returncode != 0:
        diagnostic("ACP_CHILD_EXIT_NONZERO")
    return 0 if process.returncode == 0 else 2
