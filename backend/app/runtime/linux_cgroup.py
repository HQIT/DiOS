"""Native cgroup-v2 termination without thawing a revoked workload."""

import os
import re
from pathlib import Path

from .database import RuntimeErrorCode


def kill_frozen_container(
    container_id, pid, *, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup")
):
    """Require a locally verified, frozen Docker cgroup and write its native kill file.

    Remote daemons, cgroup v1 and insufficient host permissions fail closed. The
    caller must still obtain the daemon's exit observation before completing work.
    """
    if (
        not re.fullmatch(r"[0-9a-f]{64}", container_id)
        or type(pid) is not int
        or pid <= 0
    ):
        raise RuntimeErrorCode("RUNTIME_FROZEN_STOP_UNAVAILABLE")
    try:
        membership = proc_root / str(pid) / "cgroup"
        before = membership.read_text()
        entries = [line[3:] for line in before.splitlines() if line.startswith("0::/")]
        if len(entries) != 1:
            raise ValueError("cgroup v2 required")
        relative = Path(entries[0].lstrip("/"))
        if ".." in relative.parts or relative.name not in {
            container_id,
            f"docker-{container_id}.scope",
        }:
            raise ValueError("container identity mismatch")
        root = cgroup_root.resolve(strict=True)
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root) or target == root:
            raise ValueError("invalid cgroup")
        if "frozen 1" not in (target / "cgroup.events").read_text().splitlines():
            raise ValueError("freeze not confirmed")
        fd = os.open(target / "cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW)
        try:
            if membership.read_text() != before:
                raise ValueError("process membership changed")
            os.write(fd, b"1")
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise RuntimeErrorCode("RUNTIME_FROZEN_STOP_UNAVAILABLE") from None
