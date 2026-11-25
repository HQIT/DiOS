"""Operator-pinned remote execution profile, separate from process launch specs."""

import ipaddress
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from .database import RuntimeErrorCode
from .store import digest


@dataclass(frozen=True)
class A2AProfile:
    endpoint: str
    card_url: str
    skill_id: str
    artifact_prefix: str
    allow_loopback_http: bool = False
    request_timeout: float = 10
    response_limit: int = 262144
    max_concurrent: int = 1
    recovery: dict | None = None
    execution_kind = "remote-a2a"
    control_scope = "remote-boundary"
    kind = "task"
    mounts = ()

    def validate(self):
        from .store import identifier

        identifier(self.skill_id)
        if (
            type(self.allow_loopback_http) is not bool
            or not 1 <= self.request_timeout <= 20
        ):
            raise RuntimeErrorCode("A2A_PROFILE_INVALID")
        if (
            type(self.response_limit) is not int
            or not 1024 <= self.response_limit <= 1048576
        ):
            raise RuntimeErrorCode("A2A_PROFILE_INVALID")
        if type(self.max_concurrent) is not int or not 1 <= self.max_concurrent <= 32:
            raise RuntimeErrorCode("A2A_PROFILE_INVALID")
        endpoints = [
            urlsplit(x) for x in (self.endpoint, self.card_url, self.artifact_prefix)
        ]
        for url in endpoints:
            if (
                not url.hostname
                or url.username
                or url.password
                or url.fragment
                or url.query
                or "%" in url.netloc
                or ".." in url.path.split("/")
                or "\\" in url.path
            ):
                raise RuntimeErrorCode("A2A_ENDPOINT_INVALID")
            loopback = url.hostname == "localhost"
            try:
                loopback = loopback or ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                pass
            if url.scheme != "https" and not (
                url.scheme == "http" and loopback and self.allow_loopback_http
            ):
                raise RuntimeErrorCode("A2A_TLS_REQUIRED")
        if len({(x.scheme, x.hostname, x.port) for x in endpoints}) != 1:
            raise RuntimeErrorCode("A2A_ORIGIN_MISMATCH")
        if not self.artifact_prefix.endswith("/"):
            raise RuntimeErrorCode("A2A_ARTIFACT_PREFIX_REQUIRED")

    def fingerprint(self):
        self.validate()
        return digest({"protocol": "a2a-0.3-jsonrpc", **asdict(self)})

    def recovery_policy(self):
        from .faults import policy

        value = policy(
            self.recovery
            or {
                "resource": "a2a:" + digest(self.endpoint),
                "replay_safe": False,
                "max_attempts": 3,
                "base_delay": 5,
                "max_delay": 300,
                "quota_delay": 300,
            }
        )
        if value["replay_safe"]:
            raise RuntimeErrorCode("A2A_REPLAY_UNSUPPORTED")
        return value


def read(value):
    if value.get("governance", {}).get("mode") != "e2ag":
        raise RuntimeErrorCode("WORK_AUTHORIZATION_REQUIRED")
    if any(k in value for k in ("launch", "tool_channel", "recovery")):
        raise RuntimeErrorCode("A2A_PROFILE_INVALID")
    try:
        spec = A2AProfile(**value["remote"])
        spec.validate()
        spec.recovery_policy()
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, RuntimeErrorCode):
            raise
        raise RuntimeErrorCode("A2A_PROFILE_INVALID") from None
    return value["id"], spec
