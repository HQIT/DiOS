"""Capture a private operator report or recompute it without database access."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.runtime.database import Database
from app.runtime.evidence_report import capture, markdown, summarize


def private_file(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, help="Recompute an existing capture; no database access"
    )
    parser.add_argument("--database-url-env", default="DIOS_RUNTIME_DATABASE_URL")
    parser.add_argument("--scope")
    parser.add_argument("--profile-id")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    if not args.input and (not args.scope or not os.environ.get(args.database_url_env)):
        parser.error(
            "Capture requires --scope and a database URL in the selected environment variable"
        )
    # Explicit new private directory: no accidental replacement of prior evidence.
    args.output_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if args.input:
        data = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        db = Database(os.environ[args.database_url_env])
        try:
            data = await capture(
                db, args.scope, profile_id=args.profile_id, limit=args.limit
            )
        finally:
            await db.close()
    report = summarize(data)
    for name, value in (("capture.json", data), ("report.json", report)):
        private_file(
            args.output_directory / name,
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        )
    private_file(args.output_directory / "report.md", markdown(report))
    print(
        json.dumps(
            {
                "runs": data["selected_runs"],
                "matching_runs": data["matching_runs"],
                "bounded_records_complete": report["bounded_records_complete"],
                "capture_sha256": report["capture_sha256"],
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
