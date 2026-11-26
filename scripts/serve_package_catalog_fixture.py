"""Isolated browser qualification: real catalog API and synthetic package records."""

import argparse
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
from app.api.os import runtime_tasks
from app.runtime.database import Database
from app.runtime.package_registry import PackageRegistry
from app.runtime.package_admission import PackageAdmission
from app.runtime.object_lifetime import ObjectLifetime
from app.services.task_console import TaskConsole
from test_package_lock import package
from package_admission_fixtures import declared, admit_fixture

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--port", type=int, default=13344)
args = parser.parse_args()
args.root.mkdir(parents=True, exist_ok=True)
runtime_tasks.settings.runtime_console_enabled = True
runtime_tasks.settings.access_token = "catalog-fixture-token"


@asynccontextmanager
async def lifespan(app):
    if (args.root / "state.sqlite").exists():
        raise RuntimeError("Fixture already exists")
    db = Database("sqlite+aiosqlite:///" + str(args.root / "state.sqlite"))
    await db.migrate()
    (args.root / "profiles").mkdir()
    (args.root / "blobs").mkdir()
    console = TaskConsole(
        db, "catalog-fixture", args.root / "profiles", args.root / "artifacts"
    )
    app.state.runtime_console = console
    data = "公开示例能力包，仅供目录验证。".encode()
    (args.root / "blobs" / hashlib.sha256(data).hexdigest()).write_bytes(data)
    registry = PackageRegistry(db, console.scope)
    refs = []
    for number in range(1, 25):
        pkg = declared(
            package("catalog-agent", str(number), content=data, path="usage.txt")
        )
        pkg["kind"] = "skill" if number % 2 else "tool"
        saved = await registry.register(
            pkg,
            args.root / "blobs",
            console.provider,
            key=str(number),
            actor="fixture-publisher",
        )
        refs.append(saved["digest"])
        if number in (1, 3, 4, 5):
            review = await admit_fixture(db, console.scope, args.root, pkg)
            if number in (3, 4):
                await PackageAdmission(db, console.scope).review(
                    pkg,
                    "rejected" if number == 3 else "revoked",
                    review["statement"],
                    expected_version=1,
                    key="decision-" + str(number),
                    actor="fixture-reviewer",
                )
            if number == 5:
                ref = review["statement"]["predicate"]["checks"]["source"]["evidence"]
                await ObjectLifetime(db, console.scope).change(
                    ref,
                    expected_revision=0,
                    key="expire-evidence",
                    actor="fixture-reviewer",
                    delete=True,
                )
    (args.root / "ready.json").write_text(
        json.dumps({"refs": refs, "scope": console.scope})
    )
    try:
        yield
    finally:
        console.provider.close()
        await db.close()


app = FastAPI(lifespan=lifespan)
app.include_router(runtime_tasks.router, prefix="/api/os")
app.include_router(runtime_tasks.router, prefix="/dios/api/os")


@app.get("/dios/api/auth/status")
async def status():
    return {"access_token_required": True}


app.mount("/dios", StaticFiles(directory=ROOT / "frontend/dist", html=True))
uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
