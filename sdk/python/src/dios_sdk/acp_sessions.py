"""ACP v1 capability negotiation and isolated conversation inspection.

Inspection restores context only. It never sends a prompt, attaches tools, emits
Worker result frames, or transfers an execution grant to another process.
"""

import hashlib
from pathlib import PurePosixPath, PureWindowsPath

from acp.exceptions import RequestError

from .acp import AcpClient
from .protocol import WorkloadError, encoded


def session_capabilities(initialized):
    if (
        not isinstance(initialized, dict)
        or type(initialized.get("protocolVersion")) is not int
    ):
        raise WorkloadError("ACP_VERSION_UNSUPPORTED")
    if initialized["protocolVersion"] != 1:
        raise WorkloadError("ACP_VERSION_UNSUPPORTED")
    agent = initialized.get("agentCapabilities", {})
    if not isinstance(agent, dict):
        raise WorkloadError("ACP_FRAME_INVALID")
    sessions = agent.get("sessionCapabilities", {})
    if not isinstance(sessions, dict):
        raise WorkloadError("ACP_FRAME_INVALID")
    return {
        "load": agent.get("loadSession") is True,
        **{
            name: isinstance(sessions.get(name), dict)
            for name in ("resume", "list", "close")
        },
    }


class AcpSessionInspector(AcpClient):
    """One context inspection on a fresh, operator-owned isolated ACP process.

    The caller authorizes access to the retained state before spawning the process.
    Do not attach this client to an active executor. Even after a timeout, late
    history is retained separately and no prompt or tool operation is enabled.
    """

    def __init__(self, process, *, history_limit=1024 * 1024, event_limit=4096):
        super().__init__(process, emit_event=lambda event: None)
        self.history_limit = history_limit
        self.event_limit = event_limit
        self.history = []
        self.history_bytes = 0
        self.inspection_started = False
        self.inspection_method = None
        self.inspection_complete = False

    async def incoming(self, message):
        if message.get("method") == "session/update":
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise WorkloadError("ACP_FRAME_INVALID")
            if not self.session_id or params.get("sessionId") != self.session_id:
                raise WorkloadError("SESSION_CHANGED")
            update = params.get("update")
            if not isinstance(update, dict) or not isinstance(
                update.get("sessionUpdate"), str
            ):
                raise WorkloadError("ACP_FRAME_INVALID")
            size = len(encoded(update))
            if (
                self.history_bytes + size > self.history_limit
                or len(self.history) >= self.event_limit
            ):
                raise WorkloadError("ACP_HISTORY_LIMIT_EXCEEDED")
            self.history.append(update)
            self.history_bytes += size
            return
        await super().incoming(message)

    async def request(self, method, params, key):
        self.denials += 1
        if method == "session/request_permission":
            return {"outcome": {"outcome": "cancelled"}}
        raise RequestError(
            -32601, "Session inspection does not enable client operations"
        )

    async def call(self, method, params, timeout=30):
        if method not in {"initialize", "authenticate"} or self.inspection_started:
            raise WorkloadError("ACP_INSPECTION_ONLY")
        return await super().call(method, params, timeout)

    async def inspect(self, initialized, session_id, cwd, *, method=None, timeout=30):
        if self.inspection_started or self.session_id is not None:
            raise WorkloadError("ACP_INSPECTION_ALREADY_STARTED")
        caps = session_capabilities(initialized)
        method = method or ("load" if caps["load"] else "resume")
        if (
            not isinstance(method, str)
            or method not in {"load", "resume"}
            or not caps[method]
        ):
            raise WorkloadError("ACP_SESSION_RESTORE_UNSUPPORTED")
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 160:
            raise WorkloadError("SESSION_INVALID")
        if (
            not isinstance(cwd, str)
            or len(cwd) > 4096
            or "\x00" in cwd
            or not (
                PurePosixPath(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute()
            )
        ):
            raise WorkloadError("ACP_CWD_INVALID")
        self.inspection_started = True
        self.inspection_method = method
        self.session_id = session_id
        self.session_close_supported = caps["close"]
        response = await super().call(
            "session/" + method,
            {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
            timeout,
        )
        if response.get("sessionId", session_id) != session_id:
            raise WorkloadError("SESSION_CHANGED")
        self.inspection_complete = True
        return response

    def snapshot(self):
        return {
            "session_id": self.session_id,
            "method": self.inspection_method,
            "complete": self.inspection_complete,
            "history": list(self.history),
            "history_bytes": self.history_bytes,
            "history_sha256": hashlib.sha256(encoded(self.history)).hexdigest(),
            "denied_requests": self.denials,
            "execution_resumed": False,
        }
