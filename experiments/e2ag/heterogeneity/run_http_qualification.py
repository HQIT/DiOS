"""Signed real-socket ingress qualification before the frozen pairing."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from run_file_recovery import ROOT, run_case, save_new, append_record, tool_endpoint
from http_ingress import invoke_http

MODES = ["normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"]
CUTS = ["before_receive_commit", "after_receive_commit", "after_handoff_commit"]


def rejection_case(folder, name):
    folder.mkdir()
    secret = secrets.token_urlsafe(24)
    environment = {**os.environ, "E2AG_HTTP_SECRET": secret}
    environment.pop("E2AG_QUAL_DATABASE_URL", None)
    base = [sys.executable, str(Path(__file__).with_name("run_file_recovery.py")), "--worker",
            "--directory", str(folder), "--ingress", "http", "--mode", "normal"]
    body = {"id": "delivery-1", "subject": "Qualification task", "data": {"text": "governance receipt"}}
    if name == "reserved_source": body["source"] = "trusted/other"
    if name == "missing_field": body.pop("id")
    raw = b"{not-json" if name == "bad_json" else json.dumps(body).encode()
    expected = {"wrong_signature": "INVALID_HTTP_SIGNATURE", "unconfigured_endpoint": "UNCONFIGURED_HTTP_ENDPOINT",
                "bad_json": "INVALID_JSON", "missing_field": "INVALID_ENVELOPE_FIELDS", "reserved_source": "INVALID_ENVELOPE_FIELDS"}[name]
    records = []
    with tool_endpoint(folder) as (url, operations):
        for operation in ["setup", "consume", "snapshot"]:
            command = base + ["--op", operation, "--url", url]
            if operation == "consume":
                process = invoke_http(command, cwd=ROOT, environment=environment, folder=folder, raw=raw, secret=secret,
                    wrong_signature=name == "wrong_signature", path="/webhook/other" if name == "unconfigured_endpoint" else "/webhook/fixture")
            else:
                process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=environment, timeout=30)
            entry = {"op": operation, "exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr}
            records.append(entry)
            append_record(folder / "processes.jsonl", entry)
            assert process.returncode == 0, entry
            result = json.loads(process.stdout)
            if operation == "consume": rejected = result
            if operation == "snapshot": snapshot = result
    return {"case": name, "passed": rejected == {"rejected": True, "reason": expected}
            and all(not snapshot[k] for k in ["events", "tasks", "receipts", "grants", "invocations"])
            and not operations, "rejection": rejected, "snapshot": snapshot, "processes": records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = [(m, m, None, False) for m in MODES] + [(c, "normal", c, False) for c in CUTS] + [("identity_conflict", "normal", None, True)]
    rejections = ["wrong_signature", "unconfigured_endpoint", "bad_json", "missing_field", "reserved_source"]
    if args.smoke:
        cases, rejections = cases[:1], rejections[:1]
    sources = sorted((ROOT / "backend/app").rglob("*.py")) + sorted(Path(__file__).parent.glob("*.py"))
    save_new(output / "freeze.json", {"created_at": datetime.now(timezone.utc).isoformat(),
        "cases": [c[0] for c in cases] + rejections,
        "source_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    rows = []
    for name, mode, cut, changed in cases:
        try:
            row = run_case(output / name, mode, cut, changed, ingress="http")
        except Exception:
            import traceback
            row = {"case": name, "passed": False, "error": traceback.format_exc()}
        save_new(output / (name + ".json"), row)
        rows.append(row)
        print(json.dumps({"case": name, "passed": row["passed"], "error": row.get("error")}), flush=True)
        if not row["passed"]: break
    if all(r["passed"] for r in rows):
        for name in rejections:
            row = rejection_case(output / name, name)
            save_new(output / (name + ".json"), row)
            rows.append(row)
            print(json.dumps({"case": name, "passed": row["passed"]}), flush=True)
    passed = len(rows) == len(cases) + len(rejections) and all(r["passed"] for r in rows)
    save_new(output / "summary.json", {"status": "passed" if passed else "failed", "case_count": len(rows),
        "cases": [{"case": r["case"], "passed": r["passed"]} for r in rows]})
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
