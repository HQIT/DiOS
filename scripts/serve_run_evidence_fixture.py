"""Isolated Console fixture backed by the actual E2AG and object ledgers."""

import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
from app.api.os import runtime_tasks
from app.runtime.store import RuntimeStore, digest
from app.runtime.objects import Objects, reference
from app.services.task_inputs import CONSOLE_ORIGIN
from test_run_evidence import SQLiteEvidenceTests

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
args = parser.parse_args()
args.root.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app):
    fixture = SQLiteEvidenceTests()
    original = RuntimeStore.submit

    async def submit(store, key, value, **kw):
        return await original(
            store,
            key,
            dict(
                value,
                origin=CONSOLE_ORIGIN,
                task_kind="generic",
                payload={
                    "goal": "查看执行与来源证据",
                    "sample": {"id": "synthetic", "name": "合成证据样例"},
                },
            ),
            **kw,
        )

    with patch.object(RuntimeStore, "submit", submit):
        await fixture.asyncSetUp()
    f = fixture.f
    try:
        sample = {"id": "synthetic", "name": "合成证据样例"}
        sample["sha256"] = digest(sample)
        profiles = f.root / "profiles"
        profiles.mkdir()
        (profiles / "fixture.json").write_text(
            json.dumps(
                dict(
                    id="test-profile",
                    bridge="jsonl-v1",
                    governance={"mode": "e2ag"},
                    launch={"image": "test-image", "command": ["test-command"]},
                    console={
                        "enabled": True,
                        "input_kind": "generic",
                        "name": "证据关联样例",
                        "synthetic": True,
                        "input_catalog_data": {"version": 1, "inputs": [sample]},
                    },
                )
            )
        )
        item = await f.approve(await f.request("unknown"))
        f.transport.failure = True
        await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        await f.request("pending")
        artifact = await fixture.artifact("合成来源核验记录".encode())
        objects = Objects(f.db, f.scope)
        source = await objects.publish(
            f.provider, [b"version one"], "source", "operator", media_type="text/plain"
        )
        await objects.link(
            reference("artifact", artifact), "derived_from", source["ref"], "operator"
        )
        await objects.link(
            source["ref"],
            "associated_with",
            {"namespace": "knowledge", "key": "manual", "version": "v1"},
            "operator",
        )
        await objects.publish(
            f.provider,
            [b"version two"],
            "next",
            "operator",
            object_id=source["ref"]["id"],
            expected_version=1,
        )
        app.state.runtime_console = fixture.console
        (args.root / "ready.json").write_text(
            json.dumps({"run_id": f.run["id"], "source": source["ref"]})
        )
        with (
            patch.object(runtime_tasks.settings, "runtime_console_enabled", True),
            patch.object(runtime_tasks.settings, "access_token", "operator-test"),
        ):
            yield
    finally:
        await fixture.asyncTearDown()


app = FastAPI(lifespan=lifespan)
app.include_router(runtime_tasks.router, prefix="/dios/api/os")


@app.get("/dios/api/auth/status")
async def status():
    return {"access_token_required": True}


app.mount("/dios", StaticFiles(directory=ROOT / "frontend/dist", html=True))
uvicorn.run(app, host="127.0.0.1", port=13348, log_level="warning")
