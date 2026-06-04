"""访问 Docker 内网服务（DiAgent 等），绕过宿主机注入的 HTTP_PROXY。"""

from __future__ import annotations

import httpx


def internal_client(**kwargs) -> httpx.AsyncClient:
    kwargs.setdefault("trust_env", False)
    return httpx.AsyncClient(**kwargs)
