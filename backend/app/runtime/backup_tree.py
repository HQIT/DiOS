"""Bounded POSIX tree capture through directory handles; never follow Agent symlinks."""

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from .database import RuntimeErrorCode
from .recovery import _sync_dir, _write


def inspect_tree(
    source, budget, *, destination=None, max_bytes=1024**3, max_files=20000
):
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeErrorCode("BACKUP_TREE_PROVIDER_UNSUPPORTED")
    source = Path(source)
    if source.is_symlink():
        raise RuntimeErrorCode("BACKUP_TREE_SYMLINK")
    files, directories, total = {}, [], 0

    def visit(fd, relative):
        nonlocal total
        names = sorted(os.listdir(fd))
        for name in names:
            budget()
            child = relative / name
            path = child.as_posix()
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if len(files) + len(directories) >= max_files:
                raise RuntimeErrorCode("BACKUP_TREE_LIMIT")
            if stat.S_ISDIR(info.st_mode):
                directories.append(path)
                if destination is not None:
                    (destination / path).mkdir(mode=0o700)
                nested = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                try:
                    visit(nested, child)
                finally:
                    os.close(nested)
                if destination is not None:
                    _sync_dir(destination / path)
            elif stat.S_ISREG(info.st_mode):
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    before = os.fstat(handle)
                    if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                        raise RuntimeErrorCode("BACKUP_TREE_CHANGED")

                    def chunks(handle=handle):
                        nonlocal total
                        while block := os.read(handle, 65536):
                            budget()
                            total += len(block)
                            if total > max_bytes:
                                raise RuntimeErrorCode("BACKUP_TREE_LIMIT")
                            yield block

                    if destination is None:
                        hashed, size = hashlib.sha256(), 0
                        for block in chunks():
                            hashed.update(block)
                            size += len(block)
                        value = {"sha256": hashed.hexdigest(), "size": size}
                    else:
                        value = _write(destination / path, chunks())
                    after = os.fstat(handle)
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise RuntimeErrorCode("BACKUP_TREE_CHANGED")
                    files[path] = dict(value, executable=bool(before.st_mode & 0o111))
                    if destination is not None and files[path]["executable"]:
                        (destination / path).chmod(0o700)
                finally:
                    os.close(handle)
            else:
                raise RuntimeErrorCode("BACKUP_TREE_UNSUPPORTED_ENTRY")
        if names != sorted(os.listdir(fd)):
            raise RuntimeErrorCode("BACKUP_TREE_CHANGED")

    if destination is not None:
        destination = Path(destination)
        destination.mkdir(mode=0o700)
    root = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(root, PurePosixPath())
    finally:
        os.close(root)
    if destination is not None:
        _sync_dir(destination)
    return {"files": files, "directories": directories, "bytes": total}


def capture_tree(source, destination, budget, **limits):
    captured = inspect_tree(source, budget, destination=destination, **limits)
    if inspect_tree(source, budget, **limits) != captured:
        raise RuntimeErrorCode("BACKUP_TREE_CHANGED")
    return captured
