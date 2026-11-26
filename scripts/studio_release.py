"""Review and publish Studio requests through the same authenticated API as Console."""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:13081")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--after", default="")
    for command in ("show", "approve", "deny", "publish"):
        child = commands.add_parser(command)
        child.add_argument("id")
        if command != "show":
            child.add_argument("--version", type=int, required=True)
        if command in {"approve", "deny"}:
            child.add_argument("--reason", default="")
    create = commands.add_parser("request")
    for name in ("base", "draft"):
        create.add_argument("--" + name, type=Path, required=True)
    create.add_argument("--deployment", required=True)
    create.add_argument("--deployment-version", type=int, required=True)
    create.add_argument("--key", required=True)
    sample = create.add_mutually_exclusive_group(required=True)
    sample.add_argument("--sample")
    sample.add_argument("--input", type=Path)
    args = parser.parse_args()
    token = os.environ.get("DIOS_ACCESS_TOKEN", "")
    if not token:
        parser.error("DIOS_ACCESS_TOKEN is required")
    headers = {"X-DiOS-Access-Token": token}
    url = httpx.URL(args.url)
    if url.scheme != "https" and (
        url.scheme != "http" or url.host not in {"127.0.0.1", "localhost", "::1"}
    ):
        parser.error("Use HTTPS or a loopback development endpoint")
    if url.userinfo or url.query or url.fragment:
        parser.error("URL must not contain credentials, query or fragment")
    path, body, method, params = "/api/os/runtime/studio/releases", None, "GET", None

    def read(path, limit):
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Document too large")
        return data.decode("utf-8")

    try:
        if args.command == "request":
            method = "POST"
            body = {
                "base": read(args.base, 65536),
                "draft": read(args.draft, 65536),
                "deployment_id": args.deployment,
                "deployment_version": args.deployment_version,
                "idempotency_key": args.key,
            }
            body.update(
                {"input_payload": json.loads(read(args.input, 12288))}
                if args.input
                else {"sample_id": args.sample}
            )
        elif args.command == "list":
            params = {"after": args.after}
        else:
            from urllib.parse import quote

            path += "/" + quote(args.id, safe="")
            if args.command != "show":
                body, method = {"expected_version": args.version}, "POST"
                if args.command == "publish":
                    path += "/publish"
                else:
                    path += "/approval"
                    headers["X-DiOS-Approver-Token"] = os.environ.get(
                        "DIOS_APPROVER_TOKEN", ""
                    )
                    body.update(
                        decision="approved" if args.command == "approve" else "denied",
                        reason=args.reason,
                    )
        # Retries use the caller's key/version. A timeout never implies failure or
        # creates a replacement request; show/list recovers the durable receipt.
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.request(
                method,
                args.url.rstrip("/") + path,
                json=body,
                params=params,
                headers=headers,
            )
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        return 0 if response.is_success else 1
    except (OSError, ValueError, httpx.HTTPError):
        print(
            json.dumps(
                {
                    "error": "STUDIO_REQUEST_UNAVAILABLE",
                    "action": "Read the saved request before retrying with the same key/version",
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
