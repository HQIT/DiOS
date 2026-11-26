"""Plan/apply a verified Runtime backup transfer; credentials stay in an env file."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.provider_transfer import transfer


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--row-limit", type=int, default=100000)
    args = parser.parse_args()
    url = os.environ.get("DIOS_RUNTIME_TRANSFER_DATABASE_URL")
    if not url:
        raise RuntimeErrorCode("TRANSFER_TARGET_URL_REQUIRED")
    db = Database(url)
    try:
        result = await transfer(
            args.bundle,
            args.destination,
            db,
            apply=args.apply,
            row_limit=args.row_limit,
        )
        print(
            json.dumps(
                {
                    k: v
                    for k, v in result.items()
                    if k not in {"tables", "external_recovery"}
                },
                ensure_ascii=False,
            )
        )
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 -- redact all driver failures at the CLI boundary
        # Database-driver exceptions may include connection details or row values.
        print(
            json.dumps(
                {
                    "error": exc.code
                    if isinstance(exc, RuntimeErrorCode)
                    else "TRANSFER_FAILED"
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
