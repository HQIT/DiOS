"""Verify locked file bytes and freeze them through the existing OCI mount path."""

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .package_lock import fail, read_json, verify
from .store import digest

TARGET = "/opt/dios/packages"
EMPTY = hashlib.sha256(b"").hexdigest()


def tree_descriptor(lock):
    rows = {".": [".", 0o755, True, EMPTY]}
    size = 0
    for row in lock["packages"]:
        item = row["manifest"]
        for file in item["files"]:
            path = PurePosixPath(item["name"], file["path"])
            for parent in path.parents:
                name = str(parent)
                rows[name] = [name, 0o755, True, EMPTY]
            name = str(path)
            rows[name] = [name, file["mode"], False, file["digest"].split(":")[1]]
            size += file["size"]
    return {
        "target": TARGET,
        "digest": "sha256:" + digest([rows[k] for k in sorted(rows)]),
        "entries": len(rows),
        "size": size,
    }


def validate_binding(raw):
    if "package_source" in raw:
        fail("PACKAGE_SOURCE_NOT_FROZEN")
    if "package_lock" not in raw:
        return None
    if raw.get("bridge") != "jsonl-v1":
        fail("PACKAGE_BRIDGE_UNSUPPORTED")
    lock = verify(raw["package_lock"])
    expected = tree_descriptor(lock)
    matches = [m for m in raw.get("packaged_mounts", []) if m.get("target") == TARGET]
    if matches != [expected]:
        fail("PACKAGE_EXECUTION_BINDING_INVALID")
    from .revision_mounts import overlaps

    if any(
        overlaps(m["target"], TARGET) for m in raw.get("launch", {}).get("mounts", [])
    ):
        fail("PACKAGE_EXECUTION_BINDING_INVALID")
    return lock


def materialize(lock, blobs, destination):
    """No package scripts, network fetches or archive extraction during intake."""
    lock = verify(lock)
    root = Path(destination)
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    root.chmod(0o755)
    try:
        directory = os.open(blobs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for row in lock["packages"]:
                item = row["manifest"]
                for file in item["files"]:
                    name = file["digest"].split(":")[1]
                    fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_size != file["size"]
                        ):
                            fail("PACKAGE_PAYLOAD_MISMATCH")
                        content = stream.read(file["size"] + 1)
                    if (
                        len(content) != file["size"]
                        or "sha256:" + hashlib.sha256(content).hexdigest()
                        != file["digest"]
                    ):
                        fail("PACKAGE_PAYLOAD_MISMATCH")
                    target = root / item["name"] / file["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    for parent in target.relative_to(root).parents:
                        (root / parent).chmod(0o755)
                    with target.open("xb") as stream:
                        stream.write(content)
                    target.chmod(file["mode"])
        finally:
            os.close(directory)
    except OSError:
        fail("PACKAGE_PAYLOAD_UNAVAILABLE")


@contextmanager
def prepare(raw, profile_path):
    source = raw.pop("package_source", None)
    if source is None:
        validate_binding(raw)
        yield
        return
    if raw.get("bridge") != "jsonl-v1":
        fail("PACKAGE_BRIDGE_UNSUPPORTED")
    if (
        "package_lock" in raw
        or not isinstance(source, dict)
        or set(source) != {"lock", "blobs"}
        or any(not isinstance(x, str) or not x for x in source.values())
    ):
        fail("PACKAGE_SOURCE_INVALID")
    base = Path(profile_path).resolve().parent
    lock = verify(read_json(base / source["lock"]))
    with tempfile.TemporaryDirectory(prefix="dios-packages-") as staging:
        materialize(lock, base / source["blobs"], staging)
        raw["package_lock"] = lock
        raw["launch"].setdefault("mounts", []).append(
            {"source": staging, "target": TARGET, "read_only": True}
        )
        yield
        validate_binding(raw)
