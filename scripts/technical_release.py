"""Technical evaluation and promotion through the authenticated DiOS API."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:13081")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument(
        "kind", choices=["suite", "environment", "binding", "evaluation", "promotion"]
    )
    listing.add_argument("--after", default="")
    show = commands.add_parser("show")
    show.add_argument("kind")
    show.add_argument("id")
    show.add_argument("--version", type=int)
    for name in ("suite", "environment", "bind", "evaluate", "promote"):
        child = commands.add_parser(name)
        if name in {"suite", "environment", "bind"}:
            child.add_argument("id")
        child.add_argument(
            "--document",
            type=Path,
            required=True,
            help="API request JSON, including key/version where required",
        )
    for name in ("reconcile", "approve", "deny", "publish"):
        child = commands.add_parser(name)
        child.add_argument("id")
        if name != "reconcile":
            child.add_argument("--version", type=int, required=True)
        if name in {"approve", "deny"}:
            child.add_argument("--reason", default="")
    compare = commands.add_parser("compare")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    args = parser.parse_args()
    token = os.environ.get("DIOS_ACCESS_TOKEN")
    if not token:
        parser.error("DIOS_ACCESS_TOKEN is required")
    url = httpx.URL(args.url)
    if url.scheme != "https" and (
        url.scheme != "http" or url.host not in {"localhost", "127.0.0.1", "::1"}
    ):
        parser.error("Use HTTPS or loopback")
    if url.userinfo or url.query or url.fragment:
        parser.error("Endpoint must not contain credentials, query or fragment")
    headers = {"X-DiOS-Access-Token": token}
    method, body, params = "GET", None, {}
    esc = lambda value: quote(value, safe="")
    try:
        if args.command in {"list", "show"}:
            path = "/records/" + esc(args.kind)
            if args.command == "list":
                params = {"after": args.after}
            else:
                path += "/" + esc(args.id)
                if args.version is not None:
                    params = {"version": args.version}
        elif args.command == "compare":
            path = "/comparison"
            params = {"baseline": args.baseline, "candidate": args.candidate}
        elif args.command in {"suite", "environment", "bind", "evaluate", "promote"}:
            with args.document.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("Document too large")
            body = json.loads(raw)
            paths = {
                "suite": "suites",
                "environment": "environments",
                "bind": "bindings",
                "evaluate": "evaluations",
                "promote": "promotions",
            }
            path = "/" + paths[args.command]
            method = "POST"
            if hasattr(args, "id"):
                path += "/" + esc(args.id)
                method = "PUT"
        else:
            method = "POST"
            if args.command == "reconcile":
                path = "/evaluations/" + esc(args.id) + "/reconcile"
            else:
                path = "/promotions/" + esc(args.id)
                body = {"expected_version": args.version}
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
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.request(
                method,
                args.url.rstrip("/") + "/api/os/runtime/technical" + path,
                headers=headers,
                json=body,
                params=params,
            )
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        return 0 if response.is_success else 1
    except (OSError, ValueError, httpx.HTTPError):
        print(
            json.dumps(
                {
                    "error": "TECHNICAL_REQUEST_UNAVAILABLE",
                    "action": "Read the saved receipt before retrying with the same key/version",
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
