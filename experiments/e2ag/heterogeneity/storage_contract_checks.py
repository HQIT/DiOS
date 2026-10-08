"""Two-session persistence primitives; not workload or throughput samples."""
import asyncio
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from run_file_recovery import metadata, receipts
from app.db.database import Base
from app.models.tables import EventLog
from app.services.e2ag import verify_audit_chain
from app.services.e2ag_tool_gateway import _append_event_audit


async def qualify(url):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(metadata.create_all)
        async with sessions() as db:
            await db.execute(receipts.insert().values(identity="a" * 64, source="file/fixture", event_id="probe",
                digest="b" * 64, phase="received", event_log_id=""))
            db.add(EventLog(id="storage", source="file/fixture", event_type="file.received", subject="probe",
                cloud_event={}, matched_agent_ids=[], trace_id="trace-storage", status="pending", audit_chain=[]))
            await db.commit()
        gate, arrived = asyncio.Event(), 0
        async def claim():
            nonlocal arrived
            async with sessions() as db:
                arrived += 1
                if arrived == 2: gate.set()
                await asyncio.wait_for(gate.wait(), 5)
                result = await db.execute(update(receipts).where(receipts.c.identity == "a" * 64,
                    receipts.c.phase == "received").values(phase="dispatching"))
                await db.commit()
                return result.rowcount
        claims = await asyncio.wait_for(asyncio.gather(claim(), claim()), 15)
        audit_gate, audit_arrived = asyncio.Event(), 0
        async def append(actor):
            nonlocal audit_arrived
            async with sessions() as db:
                original, first = db.execute, True
                async def synchronized(statement, *args, **kwargs):
                    nonlocal first, audit_arrived
                    result = await original(statement, *args, **kwargs)
                    columns = getattr(statement, "selected_columns", None)
                    if first and columns is not None and list(columns.keys()) == ["audit_chain"]:
                        first = False
                        audit_arrived += 1
                        if audit_arrived == 2: audit_gate.set()
                        await asyncio.wait_for(audit_gate.wait(), 5)
                    return result
                with patch.object(db, "execute", synchronized):
                    await _append_event_audit(db, "storage", trace_id="trace-storage", stage="storage_probe",
                        outcome="observed", evidence={"actor": actor})
                    await db.commit()
        await asyncio.wait_for(asyncio.gather(append("writer-1"), append("writer-2")), 15)
        async with sessions() as db:
            event = await db.get(EventLog, "storage")
            phase = (await db.execute(select(receipts.c.phase))).scalar_one()
            chain = event.audit_chain
        checks = {"single_claim": sorted(claims) == [0, 1] and phase == "dispatching",
                  "audit_both_updates": len(chain) == 2 and {e["evidence"]["actor"] for e in chain} == {"writer-1", "writer-2"},
                  "audit_hashes": verify_audit_chain(chain), "simultaneous_audit_reads": audit_arrived == 2}
        return {"passed": all(checks.values()), "checks": checks, "claim_rowcounts": claims, "audit_chain": chain,
                "scope": "native conditional claim and audit CAS; two synchronized DB sessions, not performance or full multi-consumer recovery"}
    finally:
        await engine.dispose()
