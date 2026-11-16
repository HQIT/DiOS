"""向常驻 Agent 提供 OpenAI 兼容推理，供应商密钥仅出现在上游请求。"""

import json
from contextlib import AsyncExitStack

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.config import settings
from app.db.database import get_db
from app.services.e2ag_tool_gateway import service_mcp_endpoints
from app.services.http_internal import internal_client
from app.services.model_proxy import (
    MAX_OUTPUT_TOKENS, MODEL_GRANT_CHECK_PATH, MODEL_PROXY_PATH, reserve_model_call, verify_model_grant,
)

router = APIRouter()
_REQUEST_FIELDS = {
    "model", "messages", "stream", "stream_options", "temperature", "top_p", "stop",
    "tools", "tool_choice", "parallel_tool_calls", "reasoning_effort", "response_format",
}


class GrantAudience(BaseModel):
    """常驻 Agent 在启动 DSH 前校验的模型与运行身份。"""

    model: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=12)


def _require_proxy_enabled() -> None:
    if not settings.diagent_service_model_proxy or not settings.access_token:
        raise HTTPException(404, "Model inference proxy is disabled")


@router.post(MODEL_GRANT_CHECK_PATH, include_in_schema=False, status_code=204)
async def check_grant(audience: GrantAudience, request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """确认授权可以交给指定 Agent 的执行进程。"""
    _require_proxy_enabled()
    authorization = request.headers.get("authorization", "")
    turn = await verify_model_grant(db, authorization, audience.model, audience.agent_id)
    endpoints = await service_mcp_endpoints(db, turn, authorization)
    if endpoints:
        return JSONResponse({"mcp_servers": endpoints}, headers={"Cache-Control": "no-store"})
    return Response(status_code=204)


async def _read_payload(request: Request) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 1_048_576:
            raise HTTPException(413, "Model inference request is too large")
        data.extend(chunk)
    try:
        body = json.loads(data)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid model inference JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not isinstance(body.get("messages"), list):
        raise HTTPException(400, "Expected model and messages")
    if type(body.get("stream", False)) is not bool:
        raise HTTPException(400, "Invalid model inference stream option")
    maximum = body.get("max_tokens", MAX_OUTPUT_TOKENS)
    if type(maximum) is not int or maximum <= 0:
        raise HTTPException(400, "Invalid model inference max_tokens")
    return {**{key: value for key, value in body.items() if key in _REQUEST_FIELDS},
            "max_tokens": min(maximum, MAX_OUTPUT_TOKENS)}


@router.post(MODEL_PROXY_PATH, include_in_schema=False)
async def chat_completions(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """将获准的 Chat 请求转发至 DiOS 登记的固定模型地址。"""
    _require_proxy_enabled()
    payload = await _read_payload(request)
    model = await reserve_model_call(db, request.headers.get("authorization", ""), payload["model"])
    endpoint = model.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {model.api_key}"} if model.api_key else {}
    resources = AsyncExitStack()

    async def close_upstream() -> None:
        with anyio.move_on_after(5, shield=True):
            await resources.aclose()

    try:
        client = await resources.enter_async_context(internal_client(timeout=httpx.Timeout(290, connect=10)))
        upstream = await resources.enter_async_context(client.stream("POST", endpoint, json=payload, headers=headers))
        if upstream.status_code != 200:
            status = 429 if upstream.status_code == 429 else 502
            raise HTTPException(status, "Model provider rejected inference")
        if payload.get("stream"):
            async def stream():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await close_upstream()

            return StreamingResponse(stream(), media_type="text/event-stream", background=BackgroundTask(close_upstream))
        response = Response(await upstream.aread(), media_type="application/json")
    except BaseException as error:
        await close_upstream()
        if isinstance(error, (httpx.HTTPError, httpx.InvalidURL)):
            raise HTTPException(502, "Model provider is unavailable") from None
        raise
    await close_upstream()
    return response
