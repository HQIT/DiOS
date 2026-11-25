"""Isolated TCP/A2A/Worker/E2AG qualification with a real Docker process."""

import argparse
import asyncio
import hashlib
import json
import secrets
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


async def qualify(directory, image):
    import docker
    import httpx
    import uvicorn
    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import AgentCard, Message, TaskIdParams, TaskQueryParams
    from app.api.os import runtime_a2a, runtime_tasks
    from app.config import settings
    from app.runtime.database import Database
    from app.runtime.docker_adapter import DockerRuntimeAdapter
    from app.runtime.store import digest
    from app.runtime.work_authorizations import WorkAuthorizationVerifier
    from app.runtime.work_policy import FileWorkPolicy
    from app.runtime.worker import Worker, read_profile
    from app.services.task_console import TaskConsole
    from fastapi import FastAPI

    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    profiles = directory / "profiles"
    profiles.mkdir()
    daemon = docker.from_env()
    image_id = daemon.images.get(image).id
    daemon.close()
    catalog = {"version": 1, "requires_e2ag": True, "inputs": []}
    for name, delay in (("quick", 0.5), ("slow", 30)):
        row = {"id": name, "name": name, "delay": delay}
        catalog["inputs"].append({**row, "sha256": digest(row)})
    inputs = directory / "inputs.json"
    inputs.write_text(json.dumps(catalog), encoding="utf-8")
    profile = json.loads((ROOT / "examples/runtime/fixture-profile.json").read_text())
    profile.update(
        id="a2a-fixture",
        governance={"mode": "e2ag"},
        console={
            "enabled": True,
            "name": "A2A qualification",
            "implementation": "synthetic",
            "synthetic": True,
            "input_kind": "a2a-qualification",
            "input_catalog": str(inputs),
        },
    )
    profile["launch"]["image"] = image_id
    profile["launch"]["command"] = [
        "python",
        "-u",
        "-c",
        (
            "import os,json,runpy; p=json.loads(os.environ['DIOS_WORK_INPUT']); "
            "p['delay']=p['sample']['delay']; os.environ['DIOS_WORK_INPUT']=json.dumps(p); "
            "runpy.run_path('/opt/dios/fixture_agent.py',run_name='__main__')"
        ),
    ]
    path = profiles / "fixture.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    profile_id, spec = read_profile(path)
    permissions = [
        {
            "action": "runtime." + action,
            "resource_kind": "runtime_profile",
            "resource_id": profile_id,
            "resource_version": spec.fingerprint(),
            "constraint_revision": "a2a-qualification",
        }
        for action in ("execute", "accept_result")
    ]
    policy_file = directory / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policy_id": "a2a-qualification",
                "subject": "operator:qualification",
                "actor": "service:qualification",
                "profiles": {
                    profile_id: {
                        "profile_digest": spec.fingerprint(),
                        "secret_refs": [],
                        "work_ttl_seconds": 600,
                        "permissions": {
                            layer: permissions
                            for layer in ("caller", "service", "workload", "resource")
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    policy = FileWorkPolicy(policy_file)
    db = Database("sqlite+aiosqlite:///" + str(directory / "runtime.sqlite"))
    await db.migrate()
    console = TaskConsole(
        db, "a2a-qualification", profiles, directory / "objects", policy=policy
    )
    token = secrets.token_urlsafe(32)
    settings.access_token, settings.runtime_console_enabled = token, True
    app = FastAPI()
    app.state.runtime_console = console
    app.include_router(runtime_tasks.router, prefix="/api/os")
    app.include_router(runtime_a2a.router, prefix="/api/os")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = "http://127.0.0.1:" + str(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    worker_task = None
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("HTTP server did not start")
                await asyncio.sleep(0.05)
        async with httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            timeout=15,
        ) as http:
            endpoint = "/api/os/runtime/a2a/profiles/" + profile_id
            card_response = await http.get(endpoint + "/.well-known/agent-card.json")
            card_response.raise_for_status()
            card = AgentCard.model_validate(card_response.json())
            client = ClientFactory(
                ClientConfig(httpx_client=http, polling=True, streaming=False)
            ).create(card)
            cases = []
            for name in ("quick", "slow"):
                message = Message.model_validate(
                    {
                        "role": "user",
                        "messageId": name,
                        "contextId": "qualification-context",
                        "parts": [
                            {
                                "kind": "data",
                                "data": {
                                    "sample_id": name,
                                    "goal": "验证真实进程、会话与结果关联",
                                },
                            }
                        ],
                    }
                )
                sent = [event async for event in client.send_message(message)]
                task = sent[0][0]
                repeated = [event async for event in client.send_message(message)]
                assert task.id == repeated[0][0].id
                worker = Worker(
                    console.store,
                    DockerRuntimeAdapter("a2a-qualification"),
                    profile_id,
                    spec,
                    console.provider,
                    timeout=60,
                    poll_interval=0.05,
                    authorization_verifier=WorkAuthorizationVerifier(
                        console.store, policy
                    ),
                )
                worker_task = asyncio.create_task(worker.once())
                if name == "slow":
                    async with asyncio.timeout(20):
                        while True:
                            view = await console.detail(task.id)
                            if (
                                view["attempts"]
                                and view["attempts"][0]["confirmed_running"]
                                and view["attempts"][0]["session_id"]
                            ):
                                break
                            if worker_task.done():
                                raise AssertionError(
                                    "Worker finished before cancellation"
                                )
                            await asyncio.sleep(0.05)
                    pending = await client.cancel_task(TaskIdParams(id=task.id))
                    assert pending.metadata["dios"]["cancelRequested"]
                    assert pending.status.state in {"working", "canceled"}
                result = await asyncio.wait_for(worker_task, timeout=70)
                wire = await client.get_task(TaskQueryParams(id=task.id))
                expected = "completed" if name == "quick" else "canceled"
                assert wire.status.state == expected, wire
                attempt = result["attempts"][0]
                assert attempt["process_id"] and attempt["session_id"]
                assert attempt["remote"]["runtime"]["alive"] is False
                receipt = {
                    "case": name,
                    "run_id": task.id,
                    "context_id": wire.context_id,
                    "state": result["state"],
                    "a2a_state": wire.status.state,
                    "attempt_id": attempt["id"],
                    "session_id": attempt["session_id"],
                    "process_id": attempt["process_id"],
                    "exit": attempt["remote"]["runtime"],
                    "e2ag": bool(result["input"].get("work_authorization_ref")),
                    "duplicate_same_task": True,
                }
                if name == "quick":
                    artifact = wire.artifacts[0]
                    downloaded = await http.get(artifact.parts[0].root.file.uri)
                    downloaded.raise_for_status()
                    expected_digest = artifact.metadata["dios"]["sha256"]
                    assert (
                        hashlib.sha256(downloaded.content).hexdigest()
                        == expected_digest
                    )
                    receipt["artifact_sha256"] = expected_digest
                    assert "合成任务已完成" in downloaded.text
                cases.append(receipt)
            report = {
                "protocol": "A2A 0.3.0 JSON-RPC",
                "sdk": "a2a-sdk 0.3.26",
                "transport": "loopback HTTP",
                "image_id": image_id,
                "cases": cases,
                "limits": [
                    "Synthetic workload; no model call",
                    "One physical host; no TLS qualification",
                    "Outbound remote task ownership and recovery remain pending",
                ],
            }
            (directory / "evidence.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(report, ensure_ascii=False))
    finally:
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        server.should_exit = True
        await serving
        listener.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        required=True,
        type=Path,
        help="New isolated qualification directory",
    )
    parser.add_argument(
        "--image",
        default="dios-worker-fixture:local",
        help="Prebuilt runtime fixture image",
    )
    args = parser.parse_args()
    asyncio.run(qualify(args.directory.resolve(), args.image))


if __name__ == "__main__":
    main()
