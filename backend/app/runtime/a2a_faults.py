"""Explicit remote errors and HTTP retry timing; messages are never policy."""

import time
from email.utils import parsedate_to_datetime

import httpx
from a2a.client.errors import A2AClientHTTPError, A2AClientJSONRPCError

from .database import RuntimeErrorCode


class A2AFault(RuntimeErrorCode):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.retry_after = retry_after


def retry_after(value, now=None):
    if not value or len(value) > 128:
        return 0
    try:
        seconds = (
            int(value)
            if value.isascii() and value.isdigit()
            else (
                parsedate_to_datetime(value).timestamp()
                - (time.time() if now is None else now)
            )
        )
        return max(0, min(86400, seconds))
    except (ValueError, TypeError, OverflowError):
        return 0


def code(exc):
    if isinstance(exc, RuntimeErrorCode):
        return exc.code
    if isinstance(exc, A2AClientHTTPError):
        return http_code(exc.status_code) or "A2A_OBSERVATION_UNAVAILABLE"
    if isinstance(exc, httpx.TimeoutException):
        return "PROVIDER_TIMEOUT"
    if isinstance(exc, httpx.TransportError):
        return "PROVIDER_NETWORK_ERROR"
    if isinstance(exc, A2AClientJSONRPCError):
        return "A2A_REMOTE_RPC_ERROR"
    return "A2A_OBSERVATION_UNAVAILABLE"


def http_code(status):
    return {
        401: "AUTH_REMOTE_REJECTED",
        403: "AUTH_REMOTE_REJECTED",
        402: "A2A_REMOTE_PAYMENT_REQUIRED",
        408: "PROVIDER_TIMEOUT",
        429: "PROVIDER_RATE_LIMITED",
        502: "PROVIDER_NETWORK_ERROR",
        503: "PROVIDER_NETWORK_ERROR",
        504: "PROVIDER_TIMEOUT",
    }.get(status)
