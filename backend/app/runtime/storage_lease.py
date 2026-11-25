"""Cooperative Linux process leases for offline local-storage maintenance."""

import fcntl
import os
import weakref
import hashlib
import json
from pathlib import Path


class StorageLease:
    def __init__(self, path, *, exclusive=False):
        from .database import RuntimeErrorCode

        path = Path(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(
                fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            )
        except BlockingIOError:
            os.close(fd)
            raise RuntimeErrorCode("STORAGE_MAINTENANCE_BUSY") from None
        except BaseException:
            os.close(fd)
            raise
        self._release = weakref.finalize(self, os.close, fd)

    def close(self):
        self._release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def database_lock(path):
    path = Path(path).resolve()
    return path.with_name(path.name + ".maintenance.lock")


def artifacts_lock(root):
    return Path(root).resolve() / ".dios-maintenance.lock"


def database_identity(url):
    from sqlalchemy.engine import make_url

    value = make_url(url)
    identity = (
        str(Path(value.database).resolve())
        if value.get_backend_name() == "sqlite" and value.database != ":memory:"
        else value.render_as_string(hide_password=False)
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def bind_storage(provider, db):
    from .artifacts import LocalArtifacts

    if not isinstance(provider, LocalArtifacts):
        return
    root = provider.root
    identity = (
        database_identity(db.engine.url)
        if db is not None and hasattr(db, "engine")
        else "external-runtime"
    )
    with (root / ".dios-bindings.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / ".dios-databases.json"
        from .database import RuntimeErrorCode

        if path.is_symlink():
            raise RuntimeErrorCode("COLLECTION_PATH_INVALID")
        if path.exists() and path.stat().st_size > 65536:
            raise RuntimeErrorCode("COLLECTION_INVENTORY_LIMIT")
        values = json.loads(path.read_text()) if path.exists() else []
        if identity not in values:
            values.append(identity)
            if len(values) > 256:
                raise RuntimeErrorCode("COLLECTION_INVENTORY_LIMIT")
            temporary = root / ".dios-databases.tmp"
            fd = os.open(
                temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as stream:
                json.dump(sorted(values), stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
