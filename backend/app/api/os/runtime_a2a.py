"""Authenticated A2A JSON-RPC HTTP ingress using the official SDK application."""

import asyncio
import json
from typing import Annotated

from a2a.server.apps import A2AStarletteApplication
from a2a.types import (
    InvalidParamsError,
    InvalidRequestError,
    JSONParseError,
    JSONRPCErrorResponse,
    PushNotificationNotSupportedError,
    UnsupportedOperationError,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse, Response

from app.api.os.runtime_tasks import RuntimeRoute, service
from app.runtime.database import RuntimeErrorCode
from app.services.runtime_a2a import RuntimeA2A, agent_card
from app.services.task_console import TaskConsole

router = APIRouter(
    prefix="/runtime/a2a", tags=["runtime-a2a"], route_class=RuntimeRoute
)
MAX_REQUEST_BYTES = 256 * 1024
Console = Annotated[TaskConsole, Depends(service)]


def error_response(error, request_id=None):
    value = JSONRPCErrorResponse(id=request_id, error=error).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    value["id"] = request_id  # JSON-RPC requires a null id for unidentifiable requests.
    return JSONResponse(value)


def preflight(body):
    """Enforce wire requirements the pinned SDK defaults/coercions omit."""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return error_response(JSONParseError())
    if not isinstance(value, dict):
        return error_response(InvalidRequestError())
    request_id = value.get("id")
    if type(request_id) not in (str, int):
        request_id = None
    error = None
    if (
        value.get("jsonrpc") != "2.0"
        or request_id is None
        or not isinstance(value.get("method"), str)
    ):
        error = InvalidRequestError()
    elif "params" in value and not isinstance(value["params"], dict):
        error = InvalidParamsError()
    elif value["method"] in {"message/stream", "tasks/resubscribe"}:
        error = UnsupportedOperationError()
    elif value["method"] in {
        "tasks/pushNotificationConfig/set",
        "tasks/pushNotificationConfig/get",
        "tasks/pushNotificationConfig/list",
        "tasks/pushNotificationConfig/delete",
    }:
        error = PushNotificationNotSupportedError()
    if error is not None:
        return error_response(error, request_id)
    return None


class ProtocolResponse(Response):
    """Delegate ASGI response generation to the SDK after DiOS authentication."""

    def __init__(self, app, body):
        super().__init__()
        self.protocol_app, self.request_body = app, body

    async def __call__(self, scope, receive, send):
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": self.request_body,
                    "more_body": False,
                }
            return await receive()

        async def headers(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": list(message["headers"])
                    + [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ],
                }
            await send(message)

        await self.protocol_app(dict(scope), bounded_receive, headers)


async def profile_card(request, profile_id, console):
    profile = next(
        (p for p in await console.catalog() if p["id"] == profile_id),
        None,
    )
    if profile is None:
        raise RuntimeErrorCode("PROFILE_NOT_AVAILABLE")
    return agent_card(
        profile, str(request.url_for("runtime_a2a_rpc", profile_id=profile_id))
    )


@router.get("/profiles/{profile_id}/.well-known/agent-card.json")
async def card(profile_id: str, request: Request, console: Console):
    return (await profile_card(request, profile_id, console)).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


@router.post("/profiles/{profile_id}", name="runtime_a2a_rpc")
async def rpc(profile_id: str, request: Request, console: Console):
    # Enforce a real byte bound, including chunked bodies; the SDK itself checks
    # Content-Length only after decoding the JSON body.
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise HTTPException(413, "A2A request too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    invalid = preflight(body)
    if invalid is not None:
        return invalid
    runtime_url = str(request.url_for("runtime_a2a_rpc", profile_id=profile_id)).rsplit(
        "/a2a/", 1
    )[0]
    handler = RuntimeA2A(console, profile_id, runtime_url)
    # Historical queries and cancellation use object ACLs even after launch
    # registration is removed. Only discovery and submission need the catalog.
    card = agent_card(
        {
            "id": profile_id,
            "name": "DiOS Runtime",
            "description": "Durable task execution",
            "profile_digest": "1",
        },
        str(request.url_for("runtime_a2a_rpc", profile_id=profile_id)),
    )
    # Public SDK routes/build interface; HTTP does not start a Worker.
    path = request.scope["path"]
    root = request.scope.get("root_path", "")
    if root and path.startswith(root + "/"):
        path = path[len(root) :]
    app = A2AStarletteApplication(
        card, handler, max_content_length=MAX_REQUEST_BYTES
    ).build(rpc_url=path)
    return ProtocolResponse(app, body)
