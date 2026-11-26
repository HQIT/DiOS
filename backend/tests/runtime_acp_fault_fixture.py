"""Disposable ACP subprocess with real signals and an inherited stdout pipe."""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def server():
    sid = "fault-session-" + uuid4().hex
    mode = os.environ.get("ACP_FAULT_MODE", "success")
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            result = {"protocolVersion": 1}
        elif method == "session/new":
            result = {"sessionId": sid}
        elif method == "session/prompt":
            journal = os.environ.get("ACP_FAULT_JOURNAL")
            if journal:
                with open(journal, "a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps({"session": sid, "pid": os.getpid()}) + "\n"
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
            if mode == "network":
                from runtime_model_network_fixture import stream

                stream(sid)
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": {"stopReason": "end_turn"},
                        }
                    ),
                    flush=True,
                )
                continue
            if mode == "crash":
                descendant = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"]
                )
                value = "部分输出🙂; descendant=" + str(descendant.pid)
            else:
                value = json.dumps(
                    {"summary": "完成中文审查🙂", "evidence": ["fixture:source"]}
                )
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": value},
                            },
                        },
                    }
                ),
                flush=True,
            )
            if mode == "crash":
                time.sleep(0.15)
                os.kill(os.getpid(), signal.SIGKILL)
            result = {"stopReason": "end_turn"}
        else:
            raise ValueError(method)
        print(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
            flush=True,
        )


async def bridge():
    import app.runtime.acp_bridge as implementation
    from app.runtime.acp_bridge import emit, execute
    from app.runtime.database import RuntimeErrorCode

    assert (
        hashlib.sha256(Path(implementation.__file__).read_bytes()).hexdigest()
        == os.environ["ACP_EXPECTED_SOURCE_SHA256"]
    )

    payload = json.loads(os.environ.get("DIOS_WORK_INPUT", "{}"))
    os.environ["ACP_FAULT_MODE"] = payload.get("mode", "success")
    os.environ["ACP_FAULT_JOURNAL"] = "/run/qualification/prompts.jsonl"
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "config.json"
        config.write_text(
            json.dumps(
                {
                    "command": [sys.executable, __file__, "server"],
                    "cwd": directory,
                    "prompt_timeout": 150 if payload.get("mode") == "network" else 30,
                }
            )
        )
        try:
            return await execute(
                config, "read-only synthetic review", lambda value: value
            )
        except RuntimeErrorCode as exc:
            emit({"type": "error", "code": exc.code})
            return 2


async def interrupted_worker(config_path):
    """Kill only this test Worker after the durable exit receipt, before completion."""
    from app.runtime.artifacts import LocalArtifacts
    from app.runtime.database import Database
    from app.runtime.docker_adapter import DockerRuntimeAdapter
    from app.runtime.reconciliation import ExitReceipts
    from app.runtime.store import RuntimeStore
    from app.runtime.work_authorizations import WorkAuthorizationVerifier
    from app.runtime.work_policy import FileWorkPolicy
    from app.runtime.worker import Worker, read_profile

    config = json.loads(Path(config_path).read_text())
    db = Database(config["url"])
    store = RuntimeStore(db, config["scope"])
    profile_id, spec = read_profile(config["profile"])
    policy = FileWorkPolicy(config["policy"])
    saved = ExitReceipts.save

    def kill_after_receipt(self, *args, **kwargs):
        result = saved(self, *args, **kwargs)
        if result is not None:
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    ExitReceipts.save = kill_after_receipt
    worker = Worker(
        store,
        DockerRuntimeAdapter(config["scope"]),
        profile_id,
        spec,
        LocalArtifacts(config["artifacts"]),
        poll_interval=0.05,
        authorization_verifier=WorkAuthorizationVerifier(store, policy),
    )
    await worker.once()
    raise AssertionError("fault barrier was not reached")


if __name__ == "__main__":
    if sys.argv[1] == "server":
        server()
    elif sys.argv[1] == "worker":
        asyncio.run(interrupted_worker(sys.argv[2]))
    else:
        raise SystemExit(asyncio.run(bridge()))
