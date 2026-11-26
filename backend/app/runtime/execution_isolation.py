"""Explicit, fingerprinted isolation requirements for native execution adapters."""

import json
import os
from pathlib import Path, PurePosixPath
import stat

from .database import RuntimeErrorCode

POLICY_ENV = "DIOS_EXECUTION_ISOLATION"
READONLY = {"version": 1, "network": "none", "workspace": "read_only"}


def policy(value):
    if (
        not isinstance(value, dict)
        or value != READONLY
        or type(value.get("version")) is not int
    ):
        raise RuntimeErrorCode("EXECUTION_ISOLATION_UNSUPPORTED")
    return dict(READONLY)


def from_spec(spec):
    if POLICY_ENV not in spec.environment:
        return None
    try:
        return policy(json.loads(spec.environment[POLICY_ENV]))
    except (ValueError, TypeError):
        raise RuntimeErrorCode("EXECUTION_ISOLATION_INVALID") from None


def validate(spec, *, host_paths=False):
    if from_spec(spec) is None:
        return
    if (
        spec.network != "none"
        or spec.host_aliases
        or not spec.read_only_root
        or not spec.user.split(":")[0].isdigit()
        or int(spec.user.split(":")[0]) == 0
    ):
        raise RuntimeErrorCode("EXECUTION_ISOLATION_CONFLICT")
    for mount in spec.mounts:
        target = PurePosixPath(mount.target)
        allowed = (
            target == PurePosixPath("/workspace")
            or PurePosixPath("/workspace") in target.parents
            or target == PurePosixPath("/run/dios-tools")
            or PurePosixPath("/run/secrets") in target.parents
        )
        if (
            not allowed
            or not mount.read_only
            or ".." in target.parts
            or str(target) != mount.target
        ):
            raise RuntimeErrorCode("EXECUTION_ISOLATION_MOUNT_DENIED")
        if host_paths:
            source = Path(mount.source)
            try:
                resolved = source.resolve(strict=True)
                mode = source.stat().st_mode
            except OSError:
                raise RuntimeErrorCode(
                    "EXECUTION_ISOLATION_SOURCE_UNAVAILABLE"
                ) from None
            if resolved != source or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise RuntimeErrorCode("EXECUTION_ISOLATION_SOURCE_DENIED")
            if (
                target == PurePosixPath("/workspace")
                or PurePosixPath("/workspace") in target.parents
            ):
                _workspace_source(source)


def _workspace_source(source):
    """Workspace binds contain data, never host IPC sockets or device handles."""
    count = 0

    def onerror(error):
        raise error

    try:
        for directory, dirs, files in os.walk(
            source, followlinks=False, onerror=onerror
        ):
            for name in dirs + files:
                count += 1
                mode = (Path(directory) / name).lstat().st_mode
                if count > 100000 or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise RuntimeErrorCode("EXECUTION_ISOLATION_SOURCE_DENIED")
    except OSError:
        raise RuntimeErrorCode("EXECUTION_ISOLATION_SOURCE_UNAVAILABLE") from None


def verify_container(container, spec):
    """Inspect the daemon's effective config before any process starts or is reused."""
    if from_spec(spec) is None:
        return
    container.reload()
    attrs = container.attrs
    host = attrs.get("HostConfig", {})
    config = attrs.get("Config", {})
    expected = {m.target: m.source for m in spec.mounts}
    actual = attrs.get("Mounts", [])
    declared = host.get("Mounts", [])
    security = host.get("SecurityOpt") or []
    if (
        host.get("NetworkMode") != "none"
        or host.get("Privileged")
        or host.get("ReadonlyRootfs") is not True
        or config.get("User") != spec.user
        or host.get("CapAdd")
        or "ALL" not in (host.get("CapDrop") or [])
        or not any(
            x in {"no-new-privileges", "no-new-privileges:true"} for x in security
        )
        or host.get("Devices")
        or host.get("DeviceRequests")
        or host.get("ExtraHosts")
        or host.get("Tmpfs") != {"/tmp": "rw,noexec,nosuid,size=64m"}
        or host.get("PidMode") == "host"
        or host.get("IpcMode") == "host"
        or set(attrs.get("NetworkSettings", {}).get("Networks", {})) - {"none"}
    ):
        raise RuntimeErrorCode("EXECUTION_ISOLATION_DRIFT")
    if len(declared) != len(expected) or any(
        m.get("Type") != "bind"
        or m.get("ReadOnly") is not True
        or m.get("BindOptions", {}).get("NonRecursive") is not True
        for m in declared
    ):
        raise RuntimeErrorCode("EXECUTION_ISOLATION_DRIFT")
    # Image VOLUME declarations can create additional writable mounts despite a
    # read-only root filesystem. Only the declared binds and bounded /tmp remain.
    binds = []
    for mount in actual:
        if mount.get("Type") == "tmpfs" and mount.get("Destination") == "/tmp":
            continue
        if (
            mount.get("Type") != "bind"
            or mount.get("RW") is not False
            or expected.get(mount.get("Destination")) != mount.get("Source")
        ):
            raise RuntimeErrorCode("EXECUTION_ISOLATION_DRIFT")
        binds.append(mount["Destination"])
    if set(binds) != set(expected) or len(binds) != len(expected):
        raise RuntimeErrorCode("EXECUTION_ISOLATION_DRIFT")
