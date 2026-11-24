"""Bind existing Docker objects by immutable ID and validated launch identity."""

import hashlib
import json
import re
from dataclasses import asdict, replace

from docker.errors import DockerException, NotFound

from .database import RuntimeErrorCode
from .docker_adapter import DockerRuntimeAdapter, _native_call


def _inspect(client, container):
    container.reload()
    attrs = container.attrs
    config = attrs.get("Config") or {}
    env = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    mounts = [
        m for m in attrs.get("Mounts", []) if m.get("Destination") == "/workspace"
    ]
    state = attrs["State"]
    host = str(client.info().get("ID", ""))
    if not host or not state.get("StartedAt") or len(mounts) != 1:
        raise RuntimeErrorCode("ADOPTION_RUNTIME_IDENTITY_INCOMPLETE")
    snapshot = {
        key: attrs.get(key)
        for key in ("Id", "Name", "Created", "Image", "Config", "HostConfig", "Mounts")
    }
    return {
        "provider": DockerRuntimeAdapter.name,
        "native_id": container.id,
        "host": host,
        "process_instance": container.id + ":" + state["StartedAt"],
        "spec_digest": hashlib.sha256(
            json.dumps(snapshot, sort_keys=True).encode()
        ).hexdigest(),
        "name": attrs.get("Name", "").lstrip("/"),
        "run_id": (config.get("Labels") or {}).get("dios.run_id"),
        "managed_scope": (config.get("Labels") or {}).get("dios.runtime.scope"),
        "kind": (config.get("Labels") or {}).get("dios.runtime.kind"),
        "task_config": env.get("TASK_CONFIG"),
        "workspace": {k: mounts[0].get(k) for k in ("Type", "Source", "RW")},
        "controls": {
            "user": config.get("User"),
            **{
                key: (attrs.get("HostConfig") or {}).get(key)
                for key in (
                    "Privileged",
                    "ReadonlyRootfs",
                    "Memory",
                    "NanoCpus",
                    "PidsLimit",
                    "NetworkMode",
                    "CapAdd",
                    "SecurityOpt",
                )
            },
        },
    }


async def inspect_existing(adapter, native_id):
    if not isinstance(adapter, DockerRuntimeAdapter):
        raise RuntimeErrorCode("RUNTIME_ADOPTION_UNSUPPORTED")
    if adapter.client.api.base_url != "http+docker://localhost":
        raise RuntimeErrorCode("ADOPTION_LOCAL_DOCKER_REQUIRED")
    if not re.fullmatch("[0-9a-f]{64}", native_id):
        raise RuntimeErrorCode("FULL_NATIVE_ID_REQUIRED")

    def inspect():
        try:
            container = adapter.client.containers.get(native_id)
        except NotFound:
            raise RuntimeErrorCode("ADOPTION_RUNTIME_NOT_FOUND") from None
        identity = _inspect(adapter.client, container)
        observation = replace(
            adapter._observation(native_id, container), runtime_host=identity["host"]
        )
        return {"identity": identity, "observation": asdict(observation)}

    try:
        return await _native_call(inspect)
    except DockerException:
        raise RuntimeErrorCode("RUNTIME_OBSERVATION_UNAVAILABLE") from None


class BoundDockerAdapter(DockerRuntimeAdapter):
    def __init__(self, base, handle, identity):
        self.client, self.scope = base.client, base.scope
        self.handle, self.identity = handle, identity

    async def start(self, handle, spec):
        raise RuntimeErrorCode("ADOPTED_RUNTIME_START_FORBIDDEN")

    def _get(self, handle):
        if handle != self.handle:
            raise RuntimeErrorCode("RUNTIME_SCOPE_MISMATCH")
        # Check daemon identity even when the object is absent.
        if str(self.client.info().get("ID", "")) != self.identity["host"]:
            raise RuntimeErrorCode("RUNTIME_HOST_CHANGED")
        try:
            container = self.client.containers.get(self.identity["native_id"])
        except NotFound:
            return None
        current = _inspect(self.client, container)
        if current["process_instance"] != self.identity["process_instance"]:
            raise RuntimeErrorCode("PROCESS_CHANGED")
        if current != self.identity:
            raise RuntimeErrorCode("RUNTIME_BINDING_CHANGED")
        return container

    def _observation(self, handle, container):
        observed = super()._observation(handle, container)
        return replace(observed, runtime_host=self.identity["host"])
