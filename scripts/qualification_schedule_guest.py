"""Guest-side scheduled task and drain qualification in a disposable Linux VM."""

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

from app.runtime.database import Database
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.lifecycle import Lifecycle
from app.runtime.schedule_schema import occurrences, schedules
from app.runtime.schema import artifacts, runs
from app.runtime.store import digest
from app.runtime.work_auth_schema import work
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.work_policy import FileWorkPolicy
from app.runtime.worker import Worker, read_profile
from app.services.run_schedules import RunSchedules
from app.services.task_console import TaskConsole
from sqlalchemy import select

ROOT = Path("/opt/dios-qualification")
SCOPE = "schedule-qualification"
URL = "sqlite+aiosqlite:///" + str(ROOT / "data/runtime.sqlite")


async def setup():
    assert not (ROOT / "data/runtime.sqlite").exists()
    for directory in (
        "data",
        "data/objects",
        "profiles",
        "barrier",
        "workspace",
        "cli",
    ):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    image = json.loads((ROOT / "images.json").read_text())["fixture"]
    # Emit a real session before holding completion, so observed concurrency is meaningful.
    script = (
        "import json,os,pathlib,time,uuid\n"
        "session=str(uuid.uuid4())\n"
        "print(json.dumps({'type':'session','session_id':session}),flush=True)\n"
        "print(json.dumps({'type':'output','text':'重启资格：中文🙂'}),flush=True)\n"
        "deadline=time.monotonic()+180\n"
        "while not pathlib.Path('/run/fixture/release').exists():\n"
        " if time.monotonic()>deadline: raise RuntimeError('qualification barrier expired')\n"
        " time.sleep(.1)\n"
        "print(json.dumps({'type':'result','session_id':session,'result':"
        "{'summary':'重启资格完成','evidence':['qualification'],'session_id':session}}),flush=True)\n"
    )
    sample = {"id": "sample", "name": "Scheduled qualification"}
    sample["sha256"] = digest(sample)
    profile = {
        "id": "scheduled",
        "bridge": "jsonl-v1",
        "governance": {"mode": "e2ag"},
        "console": {
            "enabled": True,
            "name": "Scheduled qualification",
            "implementation": "synthetic",
            "synthetic": True,
            "input_kind": "qualification-json",
            "input_catalog_data": {
                "version": 1,
                "requires_e2ag": True,
                "inputs": [sample],
            },
        },
        "launch": {
            "image": image,
            "command": ["python", "-u", "-c", script],
            "network": "none",
            "user": "65534:65534",
            "read_only_root": True,
            "memory_bytes": 134217728,
            "cpu_count": 0.5,
            "pids_limit": 32,
            "mounts": [
                {
                    "source": str(ROOT / "barrier"),
                    "target": "/run/fixture",
                    "read_only": True,
                }
            ],
        },
    }
    path = ROOT / "profiles/scheduled.json"
    path.write_text(json.dumps(profile))
    _, spec = read_profile(path)
    permissions = [
        {
            "action": "runtime." + action,
            "resource_kind": "runtime_profile",
            "resource_id": "scheduled",
            "resource_version": spec.fingerprint(),
            "constraint_revision": "qualification",
        }
        for action in ("execute", "accept_result")
    ]
    (ROOT / "policy.json").write_text(
        json.dumps(
            {
                "version": 1,
                "policy_id": "qualification",
                "subject": "operator:qualification",
                "actor": "service:qualification",
                "profiles": {
                    "scheduled": {
                        "profile_digest": spec.fingerprint(),
                        "secret_refs": [],
                        "work_ttl_seconds": 86400,
                        "permissions": {
                            layer: permissions
                            for layer in ("caller", "service", "workload", "resource")
                        },
                    }
                },
            }
        )
    )
    env = {
        "DIOS_DATABASE_URL": "sqlite+aiosqlite:///" + str(ROOT / "data/service.sqlite"),
        "DIOS_WORKSPACE_ROOT": str(ROOT / "workspace"),
        "DIOS_HOST_WORKSPACE_ROOT": str(ROOT / "workspace"),
        "DIOS_SHARED_CLI_ROOT": str(ROOT / "cli"),
        "DIOS_HOST_SHARED_CLI_ROOT": str(ROOT / "cli"),
        "DIOS_ACCESS_TOKEN": secrets.token_urlsafe(32),
        "DIOS_E2AG_MODE": "enforce",
        "DIOS_RUNTIME_CONSOLE_ENABLED": "true",
        "DIOS_RUNTIME_SCHEDULES_ENABLED": "true",
        "DIOS_RUNTIME_DATABASE_URL": URL,
        "DIOS_RUNTIME_SCOPE": SCOPE,
        "DIOS_RUNTIME_PROFILES_DIR": str(ROOT / "profiles"),
        "DIOS_RUNTIME_ARTIFACTS_ROOT": str(ROOT / "data/objects"),
        "DIOS_RUNTIME_POLICY_FILE": str(ROOT / "policy.json"),
    }
    env_file = ROOT / "service.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
    env_file.chmod(0o600)
    db = Database(URL)
    try:
        await db.migrate()
    finally:
        await db.close()
    return {"initialized": True, "profile_digest": spec.fingerprint()}


async def main(command):
    if command == "setup":
        return await setup()
    db = Database(URL)
    policy = FileWorkPolicy(ROOT / "policy.json")
    console = TaskConsole(
        db, SCOPE, ROOT / "profiles", ROOT / "data/objects", policy=policy
    )
    manager = RunSchedules(console)
    try:
        if command == "create":
            return await manager.create(
                {
                    "profile_id": "scheduled",
                    "sample_id": "sample",
                    "input_payload": None,
                    "goal": "Scheduled restart qualification",
                },
                "* * * * *",
                "qualification",
            )
        if command in {"due", "overlap"}:
            async with db.transaction(SCOPE) as connection:
                await connection.execute(
                    schedules.update()
                    .where(schedules.c.scope == SCOPE)
                    .values(next_due=time.time() - (61 if command == "overlap" else 1))
                )
            return {"due": True}
        if command == "tick":
            return await manager.tick()
        if command == "drain":
            return await Lifecycle(db, SCOPE).begin_drain()
        if command == "resume":
            lifecycle = Lifecycle(db, SCOPE)
            return await lifecycle.resume((await lifecycle.status())["revision"])
        if command == "release":
            (ROOT / "barrier/release").touch()
            return {"released": True}
        if command == "worker":
            _, spec = read_profile(ROOT / "profiles/scheduled.json")
            adapter = DockerRuntimeAdapter(SCOPE)
            try:
                return await Worker(
                    console.store,
                    adapter,
                    "scheduled",
                    spec,
                    console.provider,
                    poll_interval=0.1,
                    authorization_verifier=WorkAuthorizationVerifier(
                        console.store, policy
                    ),
                ).serve(once=True)
            finally:
                adapter.client.close()
        if command == "status":
            async with db.read(SCOPE) as connection:
                run_ids = (
                    (
                        await connection.execute(
                            select(runs.c.id)
                            .where(runs.c.scope == SCOPE)
                            .order_by(runs.c.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                artifacts_rows = [
                    dict(row)
                    for row in (
                        await connection.execute(
                            select(artifacts).where(artifacts.c.run_id.in_(run_ids))
                        )
                    ).mappings()
                ]
                work_rows = [
                    dict(row)
                    for row in (
                        await connection.execute(
                            select(work).where(work.c.scope == SCOPE)
                        )
                    ).mappings()
                ]
                occurrence_rows = [
                    dict(row)
                    for row in (
                        await connection.execute(
                            select(occurrences).where(occurrences.c.scope == SCOPE)
                        )
                    ).mappings()
                ]
            for row in artifacts_rows:
                blob = console.provider.verify(row["storage_key"])
                row["verified_sha256"] = blob.key
            return {
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "lifecycle": await Lifecycle(db, SCOPE).status(),
                "runtime": await console.store.runtime_status(),
                "schedules": await manager.list(),
                "occurrences": occurrence_rows,
                "runs": [await console.store.get(identity) for identity in run_ids],
                "artifacts": artifacts_rows,
                "details": [await console.detail(identity) for identity in run_ids],
                "work": work_rows,
            }
        raise ValueError(command)
    finally:
        await db.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1])), ensure_ascii=False))
