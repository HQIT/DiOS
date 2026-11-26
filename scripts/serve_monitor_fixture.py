"""Isolated real runtime metrics endpoint for deployment qualification."""

import argparse, asyncio, json, os, sys
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
import uvicorn
from fastapi import FastAPI
from app.api.os import runtime_tasks
from app.runtime.monitor import LoopObservation
from app.services.task_console import TaskConsole
from test_runtime_monitor import SQLiteMonitorTests

p = argparse.ArgumentParser()
p.add_argument("--root", type=Path, required=True)
args = p.parse_args()
args.root.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app):
    f = SQLiteMonitorTests()
    await f.asyncSetUp()
    app.state.runtime_console = TaskConsole(
        f.db, f.scope, f.root / "profiles", f.root / "artifacts"
    )
    observers = [
        LoopObservation(f.db, f.scope, s)
        for s in ["run_scheduler", "subscription_scheduler"]
    ]

    async def observe():
        while True:
            state = (
                (args.root / "state").read_text().strip()
                if (args.root / "state").exists()
                else "unobserved"
            )
            if state == "healthy":
                for o in observers:
                    await o.cycle(AsyncMock())
            await asyncio.sleep(2)

    task = asyncio.create_task(observe())
    (args.root / "ready.json").write_text(
        json.dumps({"pid": os.getpid(), "scope": f.scope})
    )
    try:
        with (
            patch.object(runtime_tasks.settings, "runtime_console_enabled", True),
            patch.object(runtime_tasks.settings, "access_token", "fixture-operator"),
        ):
            yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await f.asyncTearDown()


app = FastAPI(lifespan=lifespan)
app.include_router(runtime_tasks.router, prefix="/api/os")
uvicorn.run(app, host="127.0.0.1", port=13349, log_level="error")
