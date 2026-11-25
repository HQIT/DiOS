"""Package controlled read-only mounts into a content-addressed OCI image."""

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath

from .database import RuntimeErrorCode
from .store import digest

MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 10000


def overlaps(first, second):
    a, b = PurePosixPath(first), PurePosixPath(second)
    return a == b or a in b.parents or b in a.parents


def inventory(source):
    """Reject links and special files before producing a bounded build context."""
    rows = []
    size = 0
    pending = [Path(source)]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeErrorCode("REVISION_MOUNT_UNSUPPORTED")
        if stat.S_ISREG(info.st_mode):
            size += info.st_size
        if len(rows) >= MAX_ENTRIES or size > MAX_BYTES:
            raise RuntimeErrorCode("REVISION_MOUNT_TOO_LARGE")
        rows.append((path, info))
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    pending.append(Path(entry.path))
                    if len(pending) + len(rows) > MAX_ENTRIES:
                        raise RuntimeErrorCode("REVISION_MOUNT_TOO_LARGE")
    return sorted(rows, key=lambda row: str(row[0]))


def signature(rows):
    return [
        (str(p), s.st_mode, s.st_size, s.st_ino, s.st_mtime_ns, s.st_ctime_ns)
        for p, s in rows
    ]


def build_context(raw, base_config):
    mounts = raw["launch"].get("mounts", [])
    inputs = raw.get("input_mount_targets", [])
    if (
        not isinstance(inputs, list)
        or any(not isinstance(x, str) for x in inputs)
        or len(set(inputs)) != len(inputs)
        or set(inputs) - {m["target"] for m in mounts}
    ):
        raise RuntimeErrorCode("REVISION_INPUT_MOUNTS_INVALID")
    controlled = [m for m in mounts if m["target"] not in inputs]
    if not controlled:
        return None, []
    if base_config.get("OnBuild"):
        raise RuntimeErrorCode("REVISION_BASE_ONBUILD_UNSUPPORTED")
    reserved = [
        "/tmp",
        "/proc",
        "/sys",
        "/dev",
        "/run/secrets",
        "/run/dios",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/usr",
        "/etc",
        "/root",
        "/boot",
        "/var",
    ]
    reserved.extend((base_config.get("Volumes") or {}).keys())
    for mount in mounts:
        target = mount["target"]
        if (
            not mount.get("read_only", True)
            or str(PurePosixPath(target)) != target
            or not target.startswith("/")
            or any(c in target for c in "\n\r\\")
        ):
            raise RuntimeErrorCode("REVISION_MOUNT_UNSUPPORTED")
        if mount in controlled and any(overlaps(target, path) for path in reserved):
            raise RuntimeErrorCode("REVISION_MOUNT_TARGET_SHADOWED")
        if any(
            other is not mount and overlaps(target, other["target"]) for other in mounts
        ):
            raise RuntimeErrorCode("REVISION_MOUNT_TARGET_SHADOWED")
    output = io.BytesIO()
    descriptors = []
    dockerfile = ["FROM " + raw["launch"]["image"], "USER 0"]
    total_size = total_entries = 0
    try:
        with tarfile.open(fileobj=output, mode="w") as archive:
            for index, mount in enumerate(controlled):
                source = Path(mount["source"])
                rows = inventory(source)
                total_entries += len(rows)
                total_size += sum(s.st_size for _, s in rows if stat.S_ISREG(s.st_mode))
                if total_entries > MAX_ENTRIES or total_size > MAX_BYTES:
                    raise RuntimeErrorCode("REVISION_MOUNT_TOO_LARGE")
                members = []
                for path, info in rows:
                    relative = path.relative_to(source).as_posix()
                    name = str(index) + ("/" + relative if relative != "." else "")
                    member = tarfile.TarInfo(name)
                    member.mode = (
                        0o755
                        if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111
                        else 0o644
                    )
                    if stat.S_ISDIR(info.st_mode):
                        member.type = tarfile.DIRTYPE
                        content = b""
                    else:
                        # O_NOFOLLOW protects a file swapped to a link after inventory.
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(fd, "rb") as stream:
                            opened = os.fstat(stream.fileno())
                            if (
                                not stat.S_ISREG(opened.st_mode)
                                or opened.st_ino != info.st_ino
                            ):
                                raise RuntimeErrorCode("REVISION_MOUNT_CHANGED")
                            content = stream.read(info.st_size + 1)
                        if len(content) != info.st_size:
                            raise RuntimeErrorCode("REVISION_MOUNT_CHANGED")
                        member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                    members.append(
                        [
                            relative,
                            member.mode,
                            member.isdir(),
                            hashlib.sha256(content).hexdigest(),
                        ]
                    )
                if signature(rows) != signature(inventory(source)):
                    raise RuntimeErrorCode("REVISION_MOUNT_CHANGED")
                is_dir = stat.S_ISDIR(rows[0][1].st_mode)
                target = mount["target"] + ("/" if is_dir else "")
                # A bind mount hides the previous directory. Remove it in the
                # derived image so COPY cannot merge stale base-image files.
                dockerfile.append(
                    "RUN " + json.dumps(["rm", "-rf", "--", mount["target"]])
                )
                dockerfile.append("COPY " + json.dumps([str(index), target]))
                descriptors.append(
                    {
                        "target": mount["target"],
                        "digest": "sha256:" + digest(members),
                        "entries": len(members),
                        "size": sum(
                            s.st_size for _, s in rows if stat.S_ISREG(s.st_mode)
                        ),
                    }
                )
            dockerfile.append("USER " + (base_config.get("User") or "0"))
            data = ("\n".join(dockerfile) + "\n").encode()
            member = tarfile.TarInfo("Dockerfile")
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    except OSError:
        raise RuntimeErrorCode("REVISION_MOUNT_UNAVAILABLE") from None
    output.seek(0)
    return output, descriptors


def package(client, raw):
    base = client.images.get(raw["launch"]["image"])
    context, descriptors = build_context(raw, base.attrs.get("Config", {}))
    if context is None:
        return
    try:
        image, _ = client.images.build(
            fileobj=context,
            custom_context=True,
            rm=True,
            pull=False,
            network_mode="none",
        )
    finally:
        context.close()
    raw["launch"]["image"] = image.id
    inputs = raw.get("input_mount_targets", [])
    raw["launch"]["mounts"] = [
        m for m in raw["launch"].get("mounts", []) if m["target"] in inputs
    ]
    raw["packaged_mounts"] = descriptors
