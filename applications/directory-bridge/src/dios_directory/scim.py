"""Bounded RFC 7644 resource reads with conditional HTTP validation."""
import asyncio
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import quote, urlsplit

import httpx

from .mapping import GROUP, USER, MappingError, identifier, require, decode


class SyncError(MappingError):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.retry_after = retry_after


def private_file(path, limit=65536):
    path = Path(path)
    require(path.is_absolute(), "ABSOLUTE_PRIVATE_PATH_REQUIRED")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                "PRIVATE_FILE_PERMISSIONS_REQUIRED")
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "INPUT_TOO_LARGE")
    return raw


def retry_seconds(value):
    try:
        seconds = int(value) if value.isdigit() else parsedate_to_datetime(value).timestamp() - time.time()
        return max(0, min(3600, seconds))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0


async def body(response, limit):
    raw = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65536):
        raw.extend(chunk)
        if len(raw) > limit: raise SyncError("SCIM_RESPONSE_TOO_LARGE")
    return raw


class ScimClient:
    def __init__(self, config, *, transport=None):
        url = urlsplit(config["base_url"])
        require(url.scheme in {"https", "http"} and bool(url.hostname) and not url.username
                and not url.password and not url.query and not url.fragment
                and (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1", "::1"}),
                "SCIM_HTTPS_REQUIRED")
        self.base_url = config["base_url"].rstrip("/")
        self.timeout = config.get("timeout_seconds", 5)
        require(type(self.timeout) in (int, float) and 0 < self.timeout <= 30, "INVALID_TIMEOUT")
        self.missing = config.get("missing_resources", "reject")
        require(self.missing in {"reject", "disable"}, "INVALID_MISSING_RESOURCE_POLICY")
        try: token = private_file(config["token_file"], 16384).decode("ascii").strip()
        except UnicodeError: raise SyncError("INVALID_SCIM_CREDENTIAL") from None
        require(bool(token) and not any(c.isspace() for c in token), "INVALID_SCIM_CREDENTIAL")
        self.client = httpx.AsyncClient(headers={"Authorization": "Bearer " + token,
            "Accept": "application/scim+json"}, trust_env=False, follow_redirects=False,
            timeout=self.timeout, transport=transport)

    async def close(self):
        await self.client.aclose()

    async def read(self, kind, resource_id, cached=None):
        require(kind in {"Users", "Groups"}, "INVALID_RESOURCE_KIND")
        identifier(resource_id)
        headers = {"If-None-Match": cached["etag"]} if cached and cached.get("etag") else {}
        params = {"attributes": "id,active,meta" if kind == "Users" else "id,members,meta"}
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream("GET", self.base_url + "/" + kind + "/" + quote(resource_id, safe=""),
                                              headers=headers, params=params) as response:
                    status = response.status_code
                    if status == 304:
                        require(bool(headers) and cached.get("value") is not None, "UNEXPECTED_NOT_MODIFIED")
                        require(response.headers.get("etag", cached["etag"]) == cached["etag"], "ETAG_MISMATCH")
                        return cached
                    if status == 404:
                        if self.missing != "disable": raise SyncError("SCIM_MISSING_RESOURCE_REQUIRES_POLICY")
                        error = decode(await body(response, 65536))
                        require(isinstance(error, dict) and error.get("status") == "404"
                                and isinstance(error.get("schemas"), list)
                                and "urn:ietf:params:scim:api:messages:2.0:Error" in error["schemas"],
                                "SCIM_DELETION_UNCONFIRMED")
                        return {"etag": None, "value": None}
                    if status == 429: raise SyncError("SCIM_RATE_LIMITED", retry_seconds(response.headers.get("retry-after", "")))
                    if status in (401, 403): raise SyncError("SCIM_AUTH_DENIED")
                    if status != 200:
                        raise SyncError("SCIM_UNAVAILABLE" if status >= 500 else "SCIM_RESPONSE_REJECTED",
                                        retry_seconds(response.headers.get("retry-after", "")))
                    raw = await body(response, 2 * 1024 * 1024)
                    value = decode(raw)
                    schema = USER if kind == "Users" else GROUP
                    require(isinstance(value, dict) and value.get("id") == resource_id
                            and isinstance(value.get("schemas"), list) and schema in value["schemas"], "INVALID_SCIM_RESOURCE")
                    selected = {"schemas": [schema], "id": resource_id}
                    if kind == "Users":
                        require(type(value.get("active")) is bool, "SCIM_ACTIVE_REQUIRED")
                        selected["active"] = value["active"]
                    else:
                        members = value.get("members")
                        require(isinstance(members, list) and len(members) <= 4096, "SCIM_COMPLETE_MEMBERS_REQUIRED")
                        selected["members"] = []
                        for member in members:
                            require(isinstance(member, dict) and member.get("type", "User") == "User", "NESTED_GROUP_UNSUPPORTED")
                            selected["members"].append({"value": identifier(member.get("value"))})
                    etag = response.headers.get("etag")
                    require(etag is None or (len(etag) <= 256 and "\r" not in etag and "\n" not in etag), "INVALID_ETAG")
                    return {"etag": etag, "value": selected}
        except (httpx.HTTPError, TimeoutError):
            raise SyncError("SCIM_UNAVAILABLE") from None
        except (json.JSONDecodeError, UnicodeError):
            raise SyncError("SCIM_RESPONSE_INVALID") from None
