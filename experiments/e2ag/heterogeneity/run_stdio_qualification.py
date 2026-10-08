"""Fixed native-governance qualification with actual MCP stdio children."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import traceback

from run_file_recovery import ROOT, run_case, save_new

MODES = ["normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = sorted((ROOT / "backend/app").rglob("*.py")) + sorted(Path(__file__).parent.glob("*.py"))
    save_new(output / "freeze.json", {"kind": "qualification", "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "file input/native governance/ASGI PEP/real stdio child/persistent SQLite; deterministic executor, not DiAgent",
        "python": sys.version, "cases": MODES, "mcp_protocol": "2025-06-18",
        "source_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    rows = []
    for mode in MODES:
        try:
            row = run_case(output / mode, mode, tool_transport="stdio")
        except Exception:
            row = {"case": mode, "passed": False, "error": traceback.format_exc()}
        save_new(output / (mode + ".json"), row)
        rows.append(row)
        print(json.dumps({"case": mode, "passed": row["passed"], "error": row.get("error")}), flush=True)
    save_new(output / "summary.json", {"status": "passed" if all(r["passed"] for r in rows) else "failed",
        "case_count": len(rows), "passed": sum(r["passed"] for r in rows),
        "cases": [{"case": r["case"], "passed": r["passed"]} for r in rows],
        "not_validated": ["DiAgent", "server-initiated MCP requests", "arbitrary subprocesses", "other Agent OS", "PostgreSQL"]})
    raise SystemExit(0 if all(r["passed"] for r in rows) else 1)


if __name__ == "__main__":
    main()
