"""Real isolated Console API with explicit strict and legacy execution profiles."""

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
from app.runtime.execution_isolation import READONLY
from test_runtime_task_console import TaskConsoleTests
from work_auth_fixtures import policy_fixture
from app.runtime.worker import read_profile

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
args = parser.parse_args()
args.root.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app):
    fixture = TaskConsoleTests()
    await fixture.asyncSetUp()
    try:
        run = await fixture.submit()
        await fixture.finish(run)
        value = dict(
            fixture.profile,
            id="strict",
            isolation=READONLY,
            governance={"mode": "e2ag"},
            console=dict(fixture.profile["console"], name="只读隔离执行"),
        )
        path = fixture.profiles / "strict.json"
        path.write_text(json.dumps(value))
        _, spec = read_profile(path)
        fixture.console.policy, _ = policy_fixture(fixture.root, "strict", spec)
        app.state.runtime_console = fixture.console
        (args.root / "ready.json").write_text(
            json.dumps({"profiles": ["review", "strict"], "run_id": run["id"]})
        )
        yield
    finally:
        await fixture.asyncTearDown()


app = FastAPI(lifespan=lifespan)
from app.api.os import runtime_tasks

app.include_router(runtime_tasks.router, prefix="/dios/api/os")


@app.get("/dios/api/auth/status")
async def status():
    return {"access_token_required": True}


app.mount("/dios", StaticFiles(directory=ROOT / "frontend/dist", html=True))
uvicorn.run(app, host="127.0.0.1", port=13347, log_level="warning")
