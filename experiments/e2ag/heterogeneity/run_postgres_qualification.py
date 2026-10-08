"""PostgreSQL state qualification using unchanged native governance workers."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import traceback

from run_file_recovery import ROOT, CUTS, run_case, save_new
from postgres_runtime import postgres
from storage_contract_checks import qualify as storage_checks

MODES = ["normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = [("normal", "normal", None, False), ("revoked_fresh", "revoked_fresh", None, False),
             ("approval_race", "approved", None, False)]
    cases += [(m, m, None, False) for m in MODES if m != "normal"]
    cases += [("identity_conflict", "normal", None, True)]
    cases += [(c, "normal", c, False) for c in CUTS]
    if args.smoke: cases = cases[:1]
    sources = sorted((ROOT / "backend/app").rglob("*.py")) + sorted(Path(__file__).parent.glob("*.py"))
    save_new(output / "freeze.json", {"kind": "qualification", "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "real PostgreSQL/native governance/file input/real HTTP tool; client process restarts; fixed executor",
        "cases": cases, "source_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    rows = []
    with postgres(output) as database:
        for name, mode, cut, conflict in cases:
            database_name = "e2ag_" + name
            url = asyncio.run(database.create(database_name))
            try:
                row = run_case(output / name, mode, cut, conflict, database_url=url, approval_race=name == "approval_race")
            except Exception:
                row = {"case": name, "passed": False, "error": traceback.format_exc().replace(database.password, "[REDACTED]")}
            # Even a failed case retains independent SQL state and dump.
            asyncio.run(database.export(database_name, output / name))
            save_new(output / (name + ".json"), row)
            rows.append(row)
            print(json.dumps({"case": name, "passed": row["passed"], "error": row.get("error", "")[-1200:]}), flush=True)
            if not row["passed"]:
                break  # diagnose the first backend failure, not repeated copies
        if not args.smoke and all(r["passed"] for r in rows):
            folder = output / "storage_contracts"
            folder.mkdir()
            url = asyncio.run(database.create("e2ag_storage_contracts"))
            contract_result = asyncio.run(storage_checks(url))
            save_new(output / "storage-contracts.json", contract_result)
            asyncio.run(database.export("e2ag_storage_contracts", folder))
            print(json.dumps({"storage_contracts": contract_result["passed"], "checks": contract_result["checks"]}), flush=True)
    save_new(output / "summary.json", {"status": "passed" if len(rows) == len(cases) and all(r["passed"] for r in rows) else "failed",
        "planned": len(cases), "case_count": len(rows), "passed": sum(r["passed"] for r in rows),
        "cases": [{"case": r["case"], "passed": r["passed"]} for r in rows]})
    raise SystemExit(0 if len(rows) == len(cases) and all(r["passed"] for r in rows) else 1)


if __name__ == "__main__":
    main()
