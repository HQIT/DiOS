"""Container entrypoint for Codex's standard auth.json credential file."""

import os
import sys
from pathlib import Path


def prepare_auth(source=Path("/run/secrets/codex-auth"), directory=Path("/tmp")):
    """Project an existing read-only secret into isolated writable Codex state."""
    if not source.is_file() or not directory.is_dir():
        raise ValueError("CODEX_AUTH_REFERENCE_MISSING")
    target = directory / "auth.json"
    try:
        target.symlink_to(source)
    except FileExistsError:
        if not target.is_symlink() or target.resolve() != source.resolve():
            raise ValueError("CODEX_AUTH_REFERENCE_CONFLICT") from None


def main():
    if os.environ.get("CODEX_HOME") != "/tmp" or len(sys.argv) < 2:
        raise ValueError("CODEX_STATE_CONFIGURATION_INVALID")
    prepare_auth()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
