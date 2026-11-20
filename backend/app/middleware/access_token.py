"""Access Token 门禁：环境变量 DIOS_ACCESS_TOKEN 未设置时不启用。"""

from __future__ import annotations

from hashlib import sha256
from secrets import compare_digest

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings
from app.services.model_proxy import MODEL_GRANT_CHECK_PATH, MODEL_GRANT_REFRESH_PATH, MODEL_PROXY_PATH

_PUBLIC_PREFIXES = (
    "/health",
    "/api/auth/status",
    "/api/os/events/webhook/",
    "/api/internal/e2ag/mcp/",
)
_EVENT_GRANT_ROUTES = {
    ("GET", "/api/os/events/catalog"),
    ("GET", "/api/os/connectors/source-patterns"),
    ("POST", "/api/os/events/manual"),
}


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
        if path in (MODEL_PROXY_PATH, MODEL_GRANT_CHECK_PATH, MODEL_GRANT_REFRESH_PATH) or _is_public(path) or request.method == "OPTIONS":
            return await call_next(request)

        if not path.startswith("/api/"):
            return await call_next(request)

        token = _extract_token(request)
        if token == expected:
            return await call_next(request)
        if token and (request.method, path) in _EVENT_GRANT_ROUTES:
            digest = sha256(token.encode()).hexdigest()
            grants = [(agent_id, grant) for agent_id, grant in settings.diagent_event_grants.items()
                      if compare_digest(digest, grant.token_sha256)]
            if len(grants) == 1:
                request.state.diagent_event_grant = grants[0]
                return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing access token"})
