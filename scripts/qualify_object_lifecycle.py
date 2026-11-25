"""Opt-in object qualification on a restored Cursor dataset; never starts an Agent.

Uses the current release image, real HTTP, two ephemeral principals and two
separate local storage installations. Source data is only backed up/read.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import docker
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.runtime.recovery import backup_sqlite, restore_sqlite


SERVER = '''
import json, os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from app.api.os import runtime_tasks
from app.runtime.database import Database
from app.runtime.work_policy import FileWorkPolicy
from app.services.task_console import TaskConsole
from app.middleware.access_token import AccessTokenMiddleware

root = Path(os.environ["QUALIFICATION_ROOT"])
config = json.loads((root / "server.json").read_text())
runtime_tasks.settings.runtime_console_enabled = True
runtime_tasks.settings.access_token = config["token"]

@asynccontextmanager
async def lifespan(app):
    db = Database("sqlite+aiosqlite:///" + str(root / "state.sqlite"))
    console = TaskConsole(db, config["scope"], root / "profiles", root / "objects",
                          policy=FileWorkPolicy(root / "policy.json"))
    app.state.runtime_console = console
    try:
        yield
    finally:
        console.provider.close()
        await db.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(AccessTokenMiddleware)
app.include_router(runtime_tasks.router, prefix="/api/os")
'''


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def qualify(source, output, image):
    source, output = source.resolve(), output.resolve()
    for path in (source, output):
        if not path.is_relative_to(ROOT / "workspace"):
            raise ValueError("Use isolated repository workspace paths")
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Output must be a new independent directory")
    prior = json.loads((source / "recovery-result.json").read_text())
    target = json.loads((source / "recovery-target.json").read_text())
    assert prior["passed"] and prior["state"] == "succeeded"
    scope, run_id = target["scope"], prior["run_id"]
    output.mkdir(mode=0o700)
    backup_sqlite("sqlite+aiosqlite:///" + str(source / "runtime.sqlite"),
                  source / "objects", output / "source-backup")
    first, second = output / "first", output / "second"
    asyncio.run(restore_sqlite(output / "source-backup", first))
    (output / "object_server.py").write_text(SERVER)
    tokens = {key: secrets.token_urlsafe(32) for key in ("operator", "a", "b")}
    now = time.time()
    directory = {"version": 1, "issuer": "qualification:object-lifecycle",
                 "audience": "dios-runtime", "principals": {}}
    for key in ("a", "b"):
        directory["principals"][key] = {
            "subject": "subject:" + key, "actor": "service:" + key,
            "scope": scope, "enabled": True, "revision": 1, "expires_at": now + 1800,
            "tokens": [{"sha256": hashlib.sha256(tokens[key].encode()).hexdigest(),
                        "enabled": True, "not_before": now - 1, "expires_at": now + 1800}],
            "profiles": {}, "objects": {run_id: ["run.read", "artifact.read"]}}
    client = docker.from_env()
    http = httpx.Client(trust_env=False, timeout=20)
    container = None
    base = None
    checks = []

    def request(method, path, who="operator", body=None, status=200):
        response = http.request(method, base + "/api/os/runtime/" + path,
                                headers={"Authorization": "Bearer " + tokens[who]}, json=body)
        assert response.status_code == status, (method, path, response.status_code, response.text[:300])
        assert response.headers.get("cache-control") == "no-store"
        return response

    def route(ref):
        return f"objects/{ref['kind']}/{ref['id']}/versions/{ref['version']}"

    def grant(ref, who, revision=0, key=None):
        body = {"grants": [{"principal_id": k, "actions": ["object.read", "object.download"],
                            "expires_at": now + 1800} for k in who],
                "expected_revision": revision, "idempotency_key": key or secrets.token_hex(8)}
        request("PUT", route(ref) + "/policy", body=body)
        return body

    def publish(key, content, **extra):
        return request("POST", "objects/files", body={"idempotency_key": key,
                       "content_base64": base64.b64encode(content).decode(), **extra}).json()

    def start(root):
        nonlocal container, base
        (root / "profiles").mkdir(exist_ok=True)
        write(root / "principals.json", directory)
        write(root / "policy.json", {"version": 1, "identity_catalog": str(root / "principals.json")})
        write(root / "server.json", {"scope": scope, "token": tokens["operator"]})
        container = client.containers.run(
            image, ["python", "-m", "uvicorn", "object_server:app", "--host", "0.0.0.0", "--port", "8000"],
            detach=True, user=f"{os.getuid()}:{os.getgid()}", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges"],
            ports={"8000/tcp": ("127.0.0.1", None)},
            volumes={str(output): {"bind": str(output), "mode": "rw"}},
            environment={"PYTHONPATH": "/app:" + str(output), "QUALIFICATION_ROOT": str(root)},
            tmpfs={"/tmp": "rw,noexec,nosuid,size=32m"})
        container.reload()
        port = container.attrs["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
        base = "http://127.0.0.1:" + port
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                if request("GET", "identity").status_code == 200:
                    return
            except (httpx.HTTPError, AssertionError):
                time.sleep(.2)
        raise RuntimeError("Isolated API startup failed; inspect private server log")

    def stop():
        nonlocal container
        if container is not None:
            container.stop(timeout=10)
            (output / (container.short_id + ".log")).write_bytes(container.logs())
            container.remove()
            container = None

    try:
        start(first)
        original = request("GET", "runs/" + run_id).json()
        assert original["attempts"][0]["id"] == prior["attempt_id"]
        assert original["attempts"][0]["session_id"] == prior["session_id"]
        native = next(a for a in original["artifacts"] if a["kind"] == "result")
        artifact, session = native["object_ref"], original["attempts"][0]["object_ref"]
        native_path = f"runs/{run_id}/artifacts/{native['id']}/download"
        native_bytes = request("GET", native_path).content
        assert hashlib.sha256(native_bytes).hexdigest() == native["digest"]
        for ref in (artifact, session):
            grant(ref, ["a"])
            request("GET", route(ref), "a")
            request("GET", route(ref), "b", status=404)
        assert request("GET", native_path, "a").content == native_bytes
        request("GET", native_path, "b", status=404)
        request("GET", native_path.replace("download", "preview"), "b", status=404)
        request("GET", native_path + "?format=markdown", "b", status=404)
        shared = request("GET", "runs/" + run_id, "b").json()
        assert not any(a["id"] == native["id"] for a in shared["artifacts"])
        assert shared["attempts"][0]["session_id"] is None
        checks.append("native artifact/session object ACL restricts existing Run shares and all report paths")

        data = "版本一：相同内容，独立身份。🙂\n".encode()
        one, duplicate = publish("one", data), publish("duplicate", data)
        assert one["ref"] != duplicate["ref"] and one["descriptor"] == duplicate["descriptor"]
        ref, other = one["ref"], duplicate["ref"]
        initial_grant = grant(ref, ["a"])
        grant(other, ["b"])
        for who, own, denied in (("a", ref, other), ("b", other, ref)):
            assert request("GET", route(own) + "/download", who).content == data
            request("GET", route(denied) + "/download", who, status=404)
        latest = publish("v2", "版本二".encode(), object_id=ref["id"], expected_version=1)["ref"]
        request("GET", route(latest), "a", status=404)
        grant(latest, ["b"])
        grant(ref, [], revision=1)
        request("GET", route(ref) + "/download", "a", status=404)
        request("PUT", route(ref) + "/policy", body=initial_grant)
        request("GET", route(ref) + "/download", "a", status=404)
        opaque = {"namespace": "external-app", "key": "opaque-123", "version": "v7"}
        request("POST", "objects/relations", body={"source": latest, "relation": "associated_with", "target": opaque})
        checks.append("two principals, content reuse, exact-version ACL and revoked grant replay")

        derived = publish("derived", b"derived content")["ref"]
        request("POST", "objects/relations", body={"source": derived, "relation": "derived_from", "target": ref})
        grant(derived, ["a"])
        until = time.time() + 2
        request("PUT", route(ref) + "/retention", body={"expected_revision": 0,
                "idempotency_key": "retain", "retain_until": until})
        delete = {"expected_revision": 1, "idempotency_key": "delete", "reason": "qualification"}
        early = request("POST", route(ref) + "/delete", body=delete, status=409).json()
        assert early["detail"] == "OBJECT_RETENTION_ACTIVE", early
        expiry = publish("expiry", b"expires")["ref"]
        grant(expiry, ["b"])
        expiry_at = time.time() + 2
        request("PUT", route(expiry) + "/retention", body={"expected_revision": 0,
                "idempotency_key": "expires", "expires_at": expiry_at})
        request("GET", route(expiry) + "/download", "b")
        time.sleep(max(0, expiry_at - time.time()) + .1)
        request("POST", route(ref) + "/delete", body=delete)
        request("GET", route(derived) + "/download", "a", status=404)
        request("GET", route(expiry) + "/download", "b", status=404)
        assert request("GET", route(other) + "/download", "b").content == data
        assert request("GET", route(latest) + "/download", "b").content == "版本二".encode()
        checks.append("retention blocks deletion, clock expiry and derived invalidation at actual HTTP reads")
        grant(artifact, [], revision=1)
        grant(session, [], revision=1)
        stop()

        command = [str(ROOT / "backend/.venv/bin/python"), "-m", "app.runtime.cli",
                   "--database-url", "sqlite+aiosqlite:///" + str(first / "state.sqlite"),
                   "object-collect", "--artifacts", str(first / "objects"),
                   "--actor", "operator:qualification", "--apply", "--key", "object-qualification"]
        result = subprocess.run(command, cwd=ROOT / "backend", capture_output=True, text=True, check=True)
        collection = json.loads(result.stdout)
        assert collection["collected_blobs"] == 2, collection
        assert (first / "objects" / hashlib.sha256(data).hexdigest()).is_file()
        assert not (first / "objects" / hashlib.sha256(b"derived content").hexdigest()).exists()
        assert not (first / "objects" / hashlib.sha256(b"expires").hexdigest()).exists()
        backup_sqlite("sqlite+aiosqlite:///" + str(first / "state.sqlite"), first / "objects", output / "relocation-backup")
        asyncio.run(restore_sqlite(output / "relocation-backup", second))
        start(second)
        for who in ("a", "b"):
            for denied in (ref, derived, expiry, artifact, session):
                request("GET", route(denied), who, status=404)
            request("GET", native_path, who, status=404)
        assert request("GET", route(other) + "/download", "b").content == data
        assert request("GET", route(latest) + "/download", "b").content == "版本二".encode()
        links = request("GET", route(latest) + "/relations", "b").json()["items"]
        assert links[0]["target"] == opaque
        assert request("GET", route(ref) + "/lifecycle").json()["state"] == "deleted"
        assert request("GET", route(derived) + "/lifecycle").json()["state"] == "source_unavailable"
        assert request("GET", native_path).content == native_bytes
        final = request("GET", "runs/" + run_id).json()
        assert final["attempts"][0]["id"] == prior["attempt_id"]
        assert final["attempts"][0]["session_id"] == prior["session_id"]
        assert {a["id"] for a in final["artifacts"]} == {a["id"] for a in original["artifacts"]}
        checks.append("physical collection respects shared content; relocated restored API preserves identities, ACL, tombstones and opaque links")
        report = {"passed": True, "image": image, "checks": checks, "collection": collection,
                  "cursor_run_id": run_id, "attempt_id": prior["attempt_id"], "session_id": prior["session_id"],
                  "artifact_id": native["id"], "artifact_sha256": native["digest"],
                  "source_recovery_evidence_sha256": hashlib.sha256((source / "recovery-result.json").read_bytes()).hexdigest(),
                  "file_refs": [ref, other, latest, derived, expiry], "new_model_calls": 0,
                  "scope": "Linux SQLite/LocalArtifacts; Cursor retained results; API replacement and storage relocation. No cross-Harness or vendor transcript claim."}
        write(output / "result.json", report)
        print(json.dumps(report, ensure_ascii=False))
    finally:
        stop()
        http.close()
        client.close()
        for root in (first, second):
            for name in ("server.json", "principals.json"):
                (root / name).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    qualify(args.source, args.output, args.image)
