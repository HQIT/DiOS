"""Read bounded regular files through directory descriptors, rejecting links."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from app.runtime.database import RuntimeErrorCode


@contextmanager
def directory(root, relative=""):
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeErrorCode("SECURE_WORKSPACE_READ_UNSUPPORTED")
    root = Path(root).absolute()
    parts = PurePosixPath(relative).parts
    if root.resolve() != root or any(p in {"..", "/"} for p in parts):
        raise RuntimeErrorCode("INVALID_WORKSPACE_PATH")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_file(root, relative, limit=4 * 1024 * 1024):
    path = PurePosixPath(relative)
    with directory(root, str(path.parent)) as parent:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > limit
            ):
                raise RuntimeErrorCode("WORKSPACE_FILE_INVALID")
            data = source.read(limit + 1)
            if len(data) > limit:
                raise RuntimeErrorCode("WORKSPACE_FILE_TOO_LARGE")
            return data


def remove_file(root, relative):
    path = PurePosixPath(relative)
    try:
        with directory(root, str(path.parent)) as parent:
            os.unlink(path.name, dir_fd=parent)
    except FileNotFoundError:
        pass


def read_result(root, run_id):
    base = f"output/events/{run_id}"
    try:
        return read_file(root, base + "/task_result.md", 8 * 1024 * 1024).decode(
            "utf-8"
        )
    except FileNotFoundError:
        pass
    candidates = []
    try:
        with directory(root, base) as fd, os.scandir(fd) as entries:
            for index, entry in enumerate(entries):
                if index >= 128:
                    raise RuntimeErrorCode("RESULT_DIRECTORY_TOO_LARGE")
                if entry.is_symlink():
                    raise RuntimeErrorCode("INVALID_WORKSPACE_PATH")
                if entry.is_dir(follow_symlinks=False):
                    with directory(root, base + "/" + entry.name) as child:
                        try:
                            info = os.stat(
                                "task_result.md", dir_fd=child, follow_symlinks=False
                            )
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            raise RuntimeErrorCode("WORKSPACE_FILE_INVALID")
                        candidates.append((info.st_mtime_ns, entry.name))
    except FileNotFoundError:
        return None
    if candidates:
        return read_file(
            root, base + "/" + max(candidates)[1] + "/task_result.md", 8 * 1024 * 1024
        ).decode("utf-8")
    return None
