"""Composition root for lifecycle backends; services consume adapter contracts."""

from functools import lru_cache

from docker.errors import DockerException

from app.config import settings
from app.runtime.adapter import ServiceEndpointRuntimeAdapter
from app.runtime.database import RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter


@lru_cache(maxsize=1)
def managed_adapter() -> ServiceEndpointRuntimeAdapter:
    try:
        return DockerRuntimeAdapter(settings.runtime_scope + ":managed")
    except DockerException:
        raise RuntimeErrorCode("RUNTIME_PROVIDER_UNAVAILABLE") from None
