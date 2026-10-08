"""Targeted real-PostgreSQL regression for stale expiry vs terminal decisions."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from run_file_recovery import ROOT, save_new, metadata
from postgres_runtime import postgres
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db.database import Base
from app.models.tables import EventLog, E2AGApproval
from app.services.e2ag_approval import create_approval, decide_approval


async def run(url):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    outcomes = []
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(metadata.create_all)
        for terminal in ["approved", "rejected"]:
            async with sessions() as stale:
                event = EventLog(id="exp_" + terminal[:3], source="file/fixture", event_type="file.received",
                    subject="terminal state probe", cloud_event={}, matched_agent_ids=[],
                    trace_id="trace_" + terminal, status="approval_required", audit_chain=[])
                stale.add(event)
                await stale.commit()
                held = await create_approval(stale, event)
                identity, expiry = held.id, held.expires_at
                async with sessions() as writer:
                    await decide_approval(writer, identity, decision=terminal, actor="first-reviewer")
                with patch("app.services.e2ag_approval._now", return_value=expiry + timedelta(seconds=1)):
                    try:
                        await decide_approval(stale, identity, decision="approved", actor="stale-reviewer")
                        error = "NO_REJECTION"
                    except ValueError as exc:
                        error = str(exc)
            async with sessions() as reader:
                actual = await reader.get(E2AGApproval, identity)
                event = await reader.get(EventLog, actual.event_log_id)
                outcomes.append({"terminal": terminal, "approval_id": identity, "error": error,
                    "persisted_status": actual.status, "persisted_actor": actual.actor,
                    "event_status": event.status, "expired_audit_entries": sum(e["outcome"] == "expired" for e in event.audit_chain)})
        return {"passed": all(r["error"] == "APPROVAL_ALREADY_DECIDED" and r["persisted_status"] == r["terminal"]
                    and r["persisted_actor"] == "first-reviewer" and r["expired_audit_entries"] == 0 for r in outcomes),
                "outcomes": outcomes, "scope": "two DB sessions, native approval API, controlled clock; not full task execution"}
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = sorted((ROOT / "backend/app").rglob("*.py")) + sorted(Path(__file__).parent.glob("*.py"))
    save_new(output / "freeze.json", {"created_at": datetime.now(timezone.utc).isoformat(),
        "cases": ["approved_then_stale_expiry", "rejected_then_stale_expiry"],
        "source_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    with postgres(output) as server:
        name = "e2ag_expiry_regression"
        url = asyncio.run(server.create(name))
        result = asyncio.run(run(url))
        asyncio.run(server.export(name, output))
        save_new(output / "result.json", result)
        # Read back separately exported SQL rows, not the API-returned state.
        raw = json.loads((output / "postgres-export.json").read_text(encoding="utf-8"))["tables"]
        checks = []
        for outcome in result["outcomes"]:
            approval = next(a for a in raw["approvals"] if a["id"] == outcome["approval_id"])
            event = next(e for e in raw["events"] if e["id"] == approval["event_log_id"])
            checks.append(approval["status"] == outcome["terminal"] and approval["actor"] == "first-reviewer"
                          and all(e["outcome"] != "expired" for e in event["audit_chain"]))
        save_new(output / "sql-readback.json", {"passed": all(checks), "checks": checks})
        print(json.dumps({"passed": result["passed"], "sql_readback": all(checks), "outcomes": result["outcomes"]}), flush=True)
    raise SystemExit(0 if result["passed"] and all(checks) else 1)


if __name__ == "__main__":
    main()
