"""Dedicated scrape credential; never grants a runtime principal or execution authority."""

import os
import secrets
import stat
from fastapi import HTTPException

TOKEN_ENV = "DIOS_RUNTIME_MONITOR_TOKEN_FILE"


def accepts(token, operator_token):
    path = os.environ.get(TOKEN_ENV)
    if not path or not token:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_size > 256
            ):
                raise ValueError()
            expected = stream.read(257).strip()
        if not 32 <= len(expected) <= 128 or any(
            c not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for c in expected
        ):
            raise ValueError()
        if operator_token and secrets.compare_digest(expected, operator_token.encode()):
            raise ValueError()
    except (OSError, ValueError):
        raise HTTPException(503, detail="MONITOR_CREDENTIAL_UNAVAILABLE") from None
    return secrets.compare_digest(token.encode(), expected)
