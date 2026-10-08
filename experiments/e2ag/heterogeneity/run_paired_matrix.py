"""Frozen R0--R4 shared scenarios and R5's two bounded combination checks."""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import traceback

from run_file_recovery import ROOT, save_new, run_case as direct_case
from run_amqp_qualification import broker, run_case as amqp_case
from postgres_runtime import postgres

MODES = ["normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"]
CONFIGS = {
    "R0": ("http", "streamable_http", "sqlite"),
    "R1": ("amqp", "streamable_http", "sqlite"),
    "R2": ("file", "streamable_http", "sqlite"),
    "R3": ("http", "stdio", "sqlite"),
    "R4": ("http", "streamable_http", "postgresql"),
    "R5": ("amqp", "stdio", "postgresql"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    planned = []
    for config in CONFIGS:
        cases = [(m, m, None) for m in (MODES[:1] if args.smoke else MODES)] if config != "R5" else [
            ("J01", "approved", "after_handoff_commit"), ("J02", "revoked_reconnect", None)]
        planned.extend({"config": config, "case": name, "mode": mode, "cut": cut} for name, mode, cut in cases)
    sources = sorted((ROOT / "backend/app").rglob("*.py")) + sorted(Path(__file__).parent.glob("*.py"))
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    save_new(output / "freeze.json", {"kind": "qualification_smoke" if args.smoke else "paired_matrix",
        "created_at": datetime.now(timezone.utc).isoformat(), "configs": CONFIGS, "cases": planned,
        "expected": {"normal": "one_write", "deny": "no_task", "pending": "pending_no_task", "approved": "one_write",
                     "tool": "403_no_write", "resource": "403_no_write", "revoked": "403_no_write", "expired": "403_no_write",
                     "J01": "one_approval_one_write_after_real_redelivery", "J02": "revoked_closed_session_no_new_tool_write"},
        "scope": "native governance; experimental ingress adapters; fixed task executor; actual ingress and tool transports; ASGI client-to-PEP",
        "source_sha256": hashes})
    rows = []
    with ExitStack() as stack:
        broker_dir, pg_dir = output / "broker", output / "postgres"
        broker_dir.mkdir(); pg_dir.mkdir()
        port, channel, environment, password = stack.enter_context(broker(broker_dir))
        pg = stack.enter_context(postgres(pg_dir))
        for config, (ingress, transport, backend) in CONFIGS.items():
            group = output / config
            group.mkdir()
            group_rows = []
            for case in [c for c in planned if c["config"] == config]:
                name, mode, cut = case["case"], case["mode"], case["cut"]
                folder = group / name
                db_name = f"e2ag_{config.lower()}_{name.lower()}"
                url = asyncio.run(pg.create(db_name)) if backend == "postgresql" else None
                try:
                    if ingress == "amqp":
                        row = amqp_case(folder, mode, cut, port, channel, environment, password,
                                        tool_transport=transport, database_url=url)
                    else:
                        row = direct_case(folder, mode, cut, ingress=ingress, tool_transport=transport, database_url=url)
                except Exception:
                    error = traceback.format_exc()
                    if url: error = error.replace(pg.password, "[REDACTED]")
                    row = {"case": name, "mode": mode, "passed": False, "error": error}
                finally:
                    if url and folder.exists():
                        asyncio.run(pg.export(db_name, folder))
                save_new(group / (name + ".json"), row)
                group_rows.append({"case": name, "passed": row["passed"]})
                rows.append({"config": config, "case": name, "passed": row["passed"]})
                print(json.dumps({**rows[-1], "error": row.get("error")}), flush=True)
                if not row["passed"]: break
            save_new(group / "summary.json", {"case_count": len(group_rows), "cases": group_rows,
                "status": "passed" if all(r["passed"] for r in group_rows) else "failed"})
            if not all(r["passed"] for r in group_rows): break
    changed = [path for path, digest in hashes.items() if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest]
    passed = len(rows) == len(planned) and all(r["passed"] for r in rows) and not changed
    save_new(output / "summary.json", {"status": "passed" if passed else "failed", "planned": len(planned),
        "case_count": len(rows), "cases": rows, "changed_sources": changed})
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
