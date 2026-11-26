"""Retain DSH LlmFailure in JSON-RPC error.data for the qualified 0.2.2-rc.5.

Both source and executable bundle are pinned. A different package must be reviewed
before this compatibility patch can be applied. No provider prose is classified.
"""

import argparse
import hashlib
from pathlib import Path


FILES = (
    (
        "src/session.ts",
        "471cab9dfdb6b3332b5bd6f3b9574a6d785911a06f1c922fe1117bc4ea416058",
        "inflight.reject(internalError(`turn failed: ${end.error.message}`))",
        "inflight.reject(RequestError.internalError(end.error, `turn failed: ${end.error.message}`))",
    ),
    (
        "lib/index.js",
        "7bbe3b260904b8d998167c0908d2bd96c78409ac2657f632108afd52c2cd8796",
        "inflight.reject(internalError$1(`turn failed: ${end.error.message}`))",
        "inflight.reject(RequestError.internalError(end.error, `turn failed: ${end.error.message}`))",
    ),
)


def apply(root):
    updates = []
    for relative, expected, before, after in FILES:
        path = root / relative
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Unqualified DSH ACP artifact: {relative}")
        if raw.count(before.encode()) != 1:
            raise ValueError(f"Unexpected DSH ACP error boundary: {relative}")
        updates.append((path, raw.replace(before.encode(), after.encode())))
    for path, content in updates:
        path.write_bytes(content)
        print(f"{hashlib.sha256(content).hexdigest()}  {path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    apply(parser.parse_args().package)
