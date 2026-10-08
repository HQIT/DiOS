"""Optional lifespan-owned connection pool; request identity is never shared."""
from contextlib import asynccontextmanager

import httpx


class BorrowedTransport(httpx.AsyncBaseTransport):
    """A per-request client may close its wrapper, but not the app-owned pool."""

    def __init__(self, transport: httpx.AsyncBaseTransport):
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        pass


@asynccontextmanager
async def mcp_transport_lifespan(app, *, enabled: bool):
    if not enabled:
        yield
        return
    # Match HTTPX's existing connection limits, TLS verification, and no-retry
    # behavior. Per-process/loop owner; never retain this pool in a global.
    async with httpx.AsyncHTTPTransport(
        verify=True, trust_env=False, retries=0,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5),
    ) as transport:
        app.state.e2ag_mcp_transport = transport
        try:
            yield
        finally:
            del app.state.e2ag_mcp_transport


def transport_options(request) -> dict:
    app = request.scope.get("app")
    transport = getattr(app.state, "e2ag_mcp_transport", None) if app is not None else None
    return {"transport": BorrowedTransport(transport)} if transport is not None else {}
