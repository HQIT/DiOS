"""Bounded stdio transport for the official ACP Connection.

RPC correlation and concurrent request dispatch belong to agent-client-protocol.
This transport supplies DiOS frame/request limits and ordered session updates.
"""

import asyncio
from contextvars import ContextVar
import json

from .protocol import WorkloadError, encoded

request_id = ContextVar("acp_request_id", default=None)


class BoundedTransport:
    def __init__(self, peer):
        self.peer = peer
        self.requests = set()
        self.closed = False
        self.write_lock = asyncio.Lock()

    async def send(self, message):
        async with self.write_lock:
            self.peer.process.stdin.write(encoded(message) + b"\n")
            await self.peer.process.stdin.drain()
        if "id" in message and "method" not in message:
            self.requests.discard(message["id"])

    async def receive(self):
        try:
            while not self.closed:
                try:
                    line = await self.peer.process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    raise WorkloadError("ACP_FRAME_TOO_LARGE") from None
                if not line:
                    raise WorkloadError("ACP_PROCESS_EXITED")
                if len(line) > 256 * 1024:
                    raise WorkloadError("ACP_FRAME_TOO_LARGE")
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise WorkloadError("ACP_FRAME_INVALID") from None
                if (
                    not line.endswith(b"\n")
                    or not isinstance(message, dict)
                    or message.get("jsonrpc") != "2.0"
                ):
                    raise WorkloadError("ACP_FRAME_INVALID")
                if "id" in message and (
                    type(message["id"]) not in (str, int)
                    or len(str(message["id"])) > 160
                ):
                    raise WorkloadError("ACP_FRAME_INVALID")
                if "method" in message:
                    method = message["method"]
                    if (
                        not isinstance(method, str)
                        or not method
                        or not isinstance(message.get("params", {}), dict)
                    ):
                        raise WorkloadError("ACP_FRAME_INVALID")
                    if "id" not in message and method != "$/cancel_request":
                        # Preserve wire order before permissions inspect tool metadata.
                        await self.peer.incoming(message)
                        continue
                    if "id" in message:
                        if message["id"] in self.requests or len(self.requests) >= 16:
                            raise WorkloadError("ACP_REQUEST_LIMIT_EXCEEDED")
                        self.requests.add(message["id"])
                        # Connection creates the handler Task in this context.
                        request_id.set(message["id"])
                elif "id" not in message or ("result" in message) == (
                    "error" in message
                ):
                    raise WorkloadError("ACP_FRAME_INVALID")
                elif "error" in message and not isinstance(message["error"], dict):
                    raise WorkloadError("ACP_FRAME_INVALID")
                return message
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.peer.failure = (
                exc
                if isinstance(exc, WorkloadError)
                else WorkloadError("ACP_TRANSPORT_FAILED")
            )
        # SDK disconnects and rejects every pending request without logging raw input.
        return None

    async def close(self):
        self.closed = True
        self.peer.process.stdin.close()
