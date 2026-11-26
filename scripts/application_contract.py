"""Inspect registered applications and their versioned runtime requirements."""

import argparse
import json
import os
from pathlib import Path

from dios_sdk import TaskClient, WorkloadError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:13081")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("platform")
    commands.add_parser("list")
    check = commands.add_parser("check")
    check.add_argument("revision")
    check.add_argument("--contract", type=Path)
    args = parser.parse_args()
    token = os.environ.get("DIOS_ACCESS_TOKEN")
    if not token:
        parser.error("DIOS_ACCESS_TOKEN is required")
    client = TaskClient(args.url, token, required_contract="1")
    try:
        if args.command == "platform":
            result = client.runtime_contract()
        elif args.command == "list":
            result = client.applications()
        else:
            value = None
            if args.contract:
                with args.contract.open("rb") as stream:
                    data = stream.read(16385)
                if len(data) > 16384:
                    raise ValueError("Contract too large")
                value = json.loads(data)
            result = client.application_compatibility(args.revision, contract=value)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("compatible", True) else 1
    except (ValueError, OSError, WorkloadError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc)
                    if isinstance(exc, WorkloadError)
                    else "APPLICATION_QUERY_UNAVAILABLE"
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
