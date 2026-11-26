"""Optional operator-owned checks in the Harness execution environment."""

import asyncio
import re
import shutil
from urllib.parse import urlsplit

import httpx

from .database import RuntimeErrorCode


class PreflightError(RuntimeErrorCode):
    def __init__(self, code, check):
        super().__init__(code)
        self.check = check


async def check_environment(config, environment):
    """No commands from task input, model calls, redirects, or exception text in output."""
    if config is None:
        return
    if not isinstance(config, dict) or set(config) - {"commands", "network"}:
        raise RuntimeErrorCode("ACP_CONFIG_INVALID")
    commands, probes = config.get("commands", []), config.get("network", [])
    if (
        not isinstance(commands, list)
        or not isinstance(probes, list)
        or len(commands) + len(probes) > 16
    ):
        raise RuntimeErrorCode("ACP_CONFIG_INVALID")
    # Validate the complete specification before touching a network endpoint.
    for name in commands:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
            raise RuntimeErrorCode("ACP_CONFIG_INVALID")
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) - {"id", "url", "proxy_env"}:
            raise RuntimeErrorCode("ACP_CONFIG_INVALID")
        if not isinstance(probe.get("id"), str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,80}", probe["id"]
        ):
            raise RuntimeErrorCode("ACP_CONFIG_INVALID")
        try:
            url = urlsplit(probe["url"])
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and not (url.username or url.password or url.query or url.fragment)
            )
            _ = url.port
            httpx.URL(probe["url"])
        except (KeyError, TypeError, ValueError, AttributeError, httpx.InvalidURL):
            valid = False
        name = probe.get("proxy_env")
        if not valid or (
            name is not None
            and (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", name)
            )
        ):
            raise RuntimeErrorCode("ACP_CONFIG_INVALID")
    for name in commands:
        if not environment.get("PATH") or not shutil.which(
            name, path=environment["PATH"]
        ):
            raise PreflightError("ACP_PREFLIGHT_DEPENDENCY_MISSING", name)
    for probe in probes:
        name = probe.get("proxy_env")
        proxy = environment.get(name) if name else None
        if name and not proxy:
            raise PreflightError("ACP_PREFLIGHT_PROXY_MISSING", probe["id"])
        if proxy:
            try:
                parsed = urlsplit(proxy)
                valid = parsed.scheme in {"http", "https"} and parsed.hostname
                _ = parsed.port
                httpx.URL(proxy)
            except (ValueError, httpx.InvalidURL):
                valid = False
            if not valid:
                raise PreflightError("ACP_PREFLIGHT_PROXY_INVALID", probe["id"])
        try:
            async with (
                asyncio.timeout(5),
                httpx.AsyncClient(
                    proxy=proxy, trust_env=False, follow_redirects=False, timeout=5
                ) as client,
            ):
                response = await client.head(probe["url"])
                if not 200 <= response.status_code < 300:
                    raise PreflightError("ACP_PREFLIGHT_HTTP_FAILED", probe["id"])
        except (httpx.RequestError, TimeoutError):
            raise PreflightError("ACP_PREFLIGHT_NETWORK_FAILED", probe["id"]) from None
        except (ValueError, ImportError):
            raise PreflightError("ACP_PREFLIGHT_PROXY_INVALID", probe["id"]) from None
