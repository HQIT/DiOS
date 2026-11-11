"""Immutable binary artifacts; no UTF-8 decoding at chunk boundaries."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Iterator, Protocol
from .database import RuntimeErrorCode

@dataclass(frozen=True)
class Blob:
    digest: str
    size: int
    key: str

class ArtifactProvider(Protocol):
    name: str
    def put(self, chunks: Iterable[bytes], *, expected_digest: str | None = None) -> Blob: ...
    def read(self, key: str) -> Iterator[bytes]: ...
    def verify(self, key: str) -> Blob: ...

class LocalArtifacts:
    name = 'local-content-addressed-v1'
    def __init__(self, root: Path, *, max_bytes=128 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def path(self, key):
        if not re.fullmatch('[0-9a-f]{64}', key):
            raise RuntimeErrorCode('INVALID_ARTIFACT_KEY')
        target = self.root / key
        if target.is_symlink():
            raise RuntimeErrorCode('INVALID_ARTIFACT_KEY')
        return target

    def put(self, chunks, *, expected_digest=None):
        fd, temp = tempfile.mkstemp(prefix='.upload-', dir=self.root)
        count = 0
        h = hashlib.sha256()
        try:
            with os.fdopen(fd, 'wb') as out:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise RuntimeErrorCode('BINARY_CHUNKS_REQUIRED')
                    count += len(chunk)
                    if count > self.max_bytes:
                        raise RuntimeErrorCode('ARTIFACT_TOO_LARGE')
                    h.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            digest = h.hexdigest()
            if expected_digest is not None and digest != expected_digest:
                raise RuntimeErrorCode('ARTIFACT_DIGEST_MISMATCH')
            target = self.path(digest)
            try:
                os.link(temp, target)  # atomic create, never overwrite an existing blob
            except FileExistsError:
                self.verify(digest)
            if os.name != 'nt':
                d = os.open(self.root, os.O_RDONLY)
                try: os.fsync(d)
                finally: os.close(d)
            return Blob(digest, count, digest)
        finally:
            Path(temp).unlink(missing_ok=True)

    def read(self, key):
        with self.path(key).open('rb') as source:
            while chunk := source.read(64 * 1024):
                yield chunk

    def verify(self, key):
        h = hashlib.sha256()
        count = 0
        for chunk in self.read(key):
            h.update(chunk)
            count += len(chunk)
        if h.hexdigest() != key:
            raise RuntimeErrorCode('ARTIFACT_DIGEST_MISMATCH')
        return Blob(key, count, key)
