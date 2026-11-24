"""Bounded local IPC to the native OIDC verifier; only configured JWKS URLs are fetched."""

import asyncio
import json
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
import ipaddress
import httpx
from .database import RuntimeErrorCode


def _read_config(path):
    with Path(path).open("rb") as stream:
        return stream.read(65537)


async def verify(path, binary, provider_id, token):
    try:
        if not Path(path).is_absolute() or not Path(binary).is_absolute():
            raise ValueError()
        raw = await asyncio.to_thread(_read_config, path)
        if len(raw) > 65536:
            raise ValueError()
        config = json.loads(raw)
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ValueError()
        provider = config["providers"][provider_id]
        if set(provider) != {"issuer", "client_id", "jwks_uri", "algorithms"}:
            raise ValueError()
        for key in ("issuer", "jwks_uri"):
            u = urlsplit(provider[key])
            local = u.hostname == "localhost"
            try:
                local = local or ipaddress.ip_address(u.hostname).is_loopback
            except ValueError:
                pass
            if (
                not u.hostname
                or u.username
                or u.password
                or u.query
                or u.fragment
                or u.scheme not in {"http", "https"}
                or (u.scheme == "http" and not local)
            ):
                raise ValueError()
        if (
            not isinstance(provider["client_id"], str)
            or not 1 <= len(provider["client_id"]) <= 160
        ):
            raise ValueError()
        if (
            not isinstance(provider["algorithms"], list)
            or not provider["algorithms"]
            or set(provider["algorithms"]) - {"RS256", "ES256"}
        ):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeErrorCode("LOGOUT_CONFIGURATION_UNAVAILABLE") from None
    try:
        async with (
            asyncio.timeout(3),
            httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=2
            ) as client,
        ):
            async with client.stream("GET", provider["jwks_uri"]) as response:
                if response.status_code != 200:
                    raise RuntimeErrorCode("LOGOUT_KEYS_UNAVAILABLE")
                raw = b""
                async for chunk in response.aiter_bytes():
                    raw += chunk
                    if len(raw) > 65536:
                        raise RuntimeErrorCode("LOGOUT_KEYS_UNAVAILABLE")
        jwks = json.loads(raw)
        if (
            not isinstance(jwks, dict)
            or not isinstance(jwks.get("keys"), list)
            or not 1 <= len(jwks["keys"]) <= 32
        ):
            raise ValueError()
    except (OSError, TimeoutError, httpx.HTTPError, ValueError):
        raise RuntimeErrorCode("LOGOUT_KEYS_UNAVAILABLE") from None
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(5):
            output, _ = await process.communicate(
                json.dumps(
                    {k: provider[k] for k in ("issuer", "client_id", "algorithms")}
                    | {"jwks": jwks, "token": token}
                ).encode()
            )
        if process.returncode == 2:
            raise RuntimeErrorCode("LOGOUT_TOKEN_INVALID")
        if process.returncode != 0 or len(output) > 16384:
            raise RuntimeErrorCode("LOGOUT_VERIFIER_UNAVAILABLE")
        event = json.loads(output)
        if set(event) != {
            "issuer",
            "client_id",
            "subject",
            "session_id",
            "event_id",
            "issued_at",
            "expires_at",
        }:
            raise ValueError()
        return event, hashlib.sha256(token.encode()).hexdigest()
    except RuntimeErrorCode:
        raise
    except (OSError, TimeoutError, ValueError):
        raise RuntimeErrorCode("LOGOUT_VERIFIER_UNAVAILABLE") from None
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
