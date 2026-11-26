"""Native service entrypoint around the production ACP bridge, synthetic endpoint only."""

import asyncio
import json
import sys

from app.runtime.acp_bridge import execute
from app.runtime.database import RuntimeErrorCode


async def main():
    try:
        return await execute(sys.argv[1], "read-only synthetic review", lambda value: value)
    except RuntimeErrorCode as exc:
        print(json.dumps({"type": "error", "code": exc.code}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
