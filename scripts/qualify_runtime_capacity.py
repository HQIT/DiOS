"""Opt-in isolated real Worker/container quota qualification, without model calls."""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.runtime.capacity import Capacity
from app.runtime.database import Database
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.store import RuntimeStore
from app.runtime.work_policy import FileWorkPolicy
from app.runtime.worker import read_profile


async def qualify(output, database_url=None):
    output.mkdir(parents=True, exist_ok=False)
    barrier = output / "barrier"
    barrier.mkdir(mode=0o755)
    url = database_url or "sqlite+aiosqlite:///" + str(output / "state.sqlite")
    scope = "capacity-qualification-" + str(uuid4())
    db = Database(url)
    await db.migrate()
    store, children, logs = RuntimeStore(db, scope), [], []
    capacity = Capacity(store)
    adapter = DockerRuntimeAdapter(scope)
    profile = json.loads((ROOT / "examples/runtime/fixture-profile.json").read_text())
    profile["governance"] = {"mode": "e2ag"}
    profile["launch"]["mounts"] = [
        {"source": str(barrier), "target": "/run/fixture", "read_only": True}
    ]
    # Resolve an existing fixture image to immutable identity; no network pull.
    profile["launch"]["image"] = adapter.client.images.get(
        profile["launch"]["image"]
    ).id
    profile_path, policy_path = output / "profile.json", output / "policy.json"
    profile_path.write_text(json.dumps(profile))
    profile_id, spec = read_profile(profile_path)
    permissions = [
        {
            "action": "runtime." + action,
            "resource_kind": "runtime_profile",
            "resource_id": profile_id,
            "resource_version": spec.fingerprint(),
            "constraint_revision": "capacity-qualification",
        }
        for action in ("execute", "accept_result")
    ]
    policy_value = {
        "version": 1,
        "policy_id": "capacity-qualification",
        "subject": "qualification:operator",
        "actor": "qualification:worker",
        "profiles": {
            profile_id: {
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "work_ttl_seconds": 3600,
                "permissions": {
                    layer: permissions
                    for layer in ("caller", "service", "workload", "resource")
                },
            }
        },
    }
    policy_path.write_text(json.dumps(policy_value))
    policy = FileWorkPolicy(policy_path)
    authorization = policy.admission(profile_id, spec.fingerprint(), [])

    async def submit(key, wait=False):
        return await store.submit(
            key,
            {
                "runtime_profile": profile_id,
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "payload": {"barrier": wait, "delay": 0.1},
            },
            authorization=authorization,
        )

    async def until(check, seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            assert all(p.returncode is None for p in children), "Worker exited"
            value = await check()
            if value:
                return value
            await asyncio.sleep(0.15)
        raise RuntimeError("CAPACITY_QUALIFICATION_DEADLINE")

    async def state(run_id, expected):
        run = await store.get(run_id)
        return run if run["state"] == expected else None

    try:
        await capacity.put(
            "shared", {"kind": "profiles", "profiles": [profile_id]}, 2, 0
        )
        initial = [await submit(str(n), True) for n in range(3)]
        for n in range(3):
            log = (output / f"worker-{n}.log").open("w")
            logs.append(log)
            children.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.runtime.cli",
                    "--scope",
                    scope,
                    "worker",
                    "--profile",
                    str(profile_path),
                    "--policy-file",
                    str(policy_path),
                    "--artifacts",
                    str(output / "artifacts"),
                    "--poll-interval",
                    ".1",
                    cwd=ROOT / "backend",
                    env=dict(os.environ, DIOS_RUNTIME_DATABASE_URL=url),
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                )
            )

        async def concurrent():
            status = await store.runtime_status()
            return (
                status
                if (status["actual_concurrency"] == 2 and len(status["workers"]) == 3)
                else None
            )

        running = await until(concurrent)
        assert len({a["process_id"] for a in running["attempts"]}) == 2
        assert len({a["session_id"] for a in running["attempts"]}) == 2
        assert len({w["pid"] for w in running["workers"]}) == 3
        queued = [r for r in initial if (await store.get(r["id"]))["state"] == "queued"]
        assert len(queued) == 1
        reservation = await capacity.status()
        await capacity.put(
            "shared", {"kind": "profiles", "profiles": [profile_id]}, 0, 1
        )
        assert (await store.runtime_status())["actual_concurrency"] == 2
        (barrier / "release").touch()
        completed = []
        for run in initial:
            if run["id"] != queued[0]["id"]:
                completed.append(
                    await until(lambda rid=run["id"]: state(rid, "succeeded"))
                )
        assert (await store.get(queued[0]["id"]))["state"] == "queued"
        assert (await capacity.status())["items"][0]["reserved_attempts"] == 0

        # Capacity permits admission, but changed Work policy still prevents launch.
        policy_path.write_text(json.dumps(dict(policy_value, profiles={})))
        await capacity.put(
            "shared", {"kind": "profiles", "profiles": [profile_id]}, 2, 2
        )
        denied = await until(lambda: state(queued[0]["id"], "failed"))
        assert len(denied["attempts"]) == 1
        assert not denied["attempts"][0]["process_id"]
        assert not denied["attempts"][0]["session_id"]
        assert not denied["attempts"][0]["remote"]
        assert (await capacity.status())["items"][0]["reserved_attempts"] == 0
        policy_path.write_text(json.dumps(policy_value))
        final = await submit("after-policy-restored")
        completed.append(await until(lambda: state(final["id"], "succeeded")))
        report = {
            "scope": scope,
            "provider": db.engine.dialect.name,
            "worker_process_ids": [p.pid for p in children],
            "observed_concurrency": running["actual_concurrency"],
            "registered_slots": running["registered_slots"],
            "running_attempts": running["attempts"],
            "capacity": reservation,
            "succeeded_runs": [r["id"] for r in completed],
            "denied_without_launch": denied["id"],
            "final_reserved_attempts": (await capacity.status())["items"][0][
                "reserved_attempts"
            ],
        }
        assert report["final_reserved_attempts"] == 0
        (output / "evidence.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        for p in children:
            if p.returncode is None:
                p.terminate()
        for p in children:
            try:
                await asyncio.wait_for(p.wait(), 15)
            except TimeoutError:
                p.kill()
                await p.wait()
        for log in logs:
            log.close()
        for container in adapter.client.containers.list(
            all=True, filters={"label": "dios.runtime.scope=" + scope}
        ):
            container.remove(force=True)
        adapter.client.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--postgres", action="store_true", help="use isolated DIOS_TEST_POSTGRES_URL"
    )
    args = parser.parse_args()
    url = os.environ["DIOS_TEST_POSTGRES_URL"] if args.postgres else None
    report = asyncio.run(qualify(args.output.resolve(), url))
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "provider",
                    "observed_concurrency",
                    "registered_slots",
                    "final_reserved_attempts",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
