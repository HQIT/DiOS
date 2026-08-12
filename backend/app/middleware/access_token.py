"""Access Token 门禁：环境变量 DIOS_ACCESS_TOKEN 未设置时不启用。"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings

_PUBLIC_PREFIXES = (
    "/health",
    "/api/os/events/webhook/",
    "/api/internal/e2ag/mcp/",
)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES)


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get("x-dios-access-token", "").strip()
    return header or None


class AccessTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        expected = settings.access_token
        if not expected:
            return await call_next(request)

        path = request.url.path
        if _is_public(path) or request.method == "OPTIONS":
            return await call_next(request)

        if not path.startswith("/api/"):
            return await call_next(request)

        token = _extract_token(request)
        if token != expected:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing access token"},
            )
        return await call_next(request)
