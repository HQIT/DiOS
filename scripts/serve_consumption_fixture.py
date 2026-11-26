"""Isolated real API/browser fixture for reservation and settlement visibility."""

import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
from app.api.os import runtime_tasks
from app.runtime.consumption import Consumption
from app.runtime.objects import Objects
from test_runtime_task_console import TaskConsoleTests

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
args = parser.parse_args()
args.root.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app):
    fixture = TaskConsoleTests()
    await fixture.asyncSetUp()
    app.state.runtime_console = fixture.console
    manager = Consumption(fixture.db, "one")
    await manager.put(
        "model-allowance",
        {"kind": "scope"},
        "micro:USD",
        6,
        3,
        0,
        key="account",
        actor="fixture",
        enforcement="reservation_only",
    )
    first = await fixture.finish(await fixture.submit("settled"))
    second = await fixture.finish(await fixture.submit("unreconciled"))
    obj = await Objects(fixture.db, "one").publish(
        fixture.console.provider,
        [b'{"synthetic":true,"actual":1}'],
        "evidence",
        "fixture",
    )
    await manager.settle(
        "model-allowance",
        first["attempts"][0]["id"],
        1,
        {
            "ref": obj["ref"],
            "digest": obj["descriptor"]["digest"],
            "basis": "operator_reconciliation",
        },
        key="settled",
        actor="fixture",
    )
    third = await fixture.submit("blocked")
    await manager.put(
        "strict", {"kind": "scope"}, "tokens", 100, 10, 0, key="strict", actor="fixture"
    )
    (args.root / "ready.json").write_text(
        json.dumps(
            {
                "settled": first["id"],
                "unreconciled": second["id"],
                "blocked": third["id"],
            }
        )
    )
    try:
        yield
    finally:
        await fixture.asyncTearDown()


app = FastAPI(lifespan=lifespan)
app.include_router(runtime_tasks.router, prefix="/dios/api/os")


@app.get("/dios/api/auth/status")
async def status():
    return {"access_token_required": True}


app.mount("/dios", StaticFiles(directory=ROOT / "frontend/dist", html=True))
uvicorn.run(app, host="127.0.0.1", port=13345, log_level="warning")
