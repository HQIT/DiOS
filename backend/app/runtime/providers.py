"""Composition root for lifecycle backends; services consume adapter contracts."""

from functools import lru_cache

from app.config import settings
from app.runtime.adapter import ServiceEndpointRuntimeAdapter
from app.runtime.docker_adapter import DockerRuntimeAdapter


@lru_cache(maxsize=1)
def managed_adapter() -> ServiceEndpointRuntimeAdapter:
    return DockerRuntimeAdapter(settings.runtime_scope + ":managed")
