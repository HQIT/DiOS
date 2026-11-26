"""Run the same E2AG contract on SQLite/PostgreSQL and builtin/native OPA paths."""

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest

import docker
import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from app.runtime.database import Database
from test_runtime_governance_matrix import GovernanceContract
from test_runtime_opa_integration import IMAGE as OPA_IMAGE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    client = docker.from_env()
    containers = []
    observations = []
    summaries = []
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="dios-governance-") as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            bundle = root / "policy"
            bundle.mkdir(mode=0o755)
            (bundle / ".manifest").write_text(
                json.dumps(
                    {"revision": "matrix-v1", "roots": ["dios"], "rego_version": 1}
                )
            )
            (bundle / "tools.rego").write_text(
                'package dios.tools\nimport rego.v1\ndefault allow := false\nallow if {\n every layer in ["caller", "service", "workload", "resource"] {\n input.context.permission in input.context.permissions[layer]\n }\n}\n'
            )
            opa = client.containers.run(
                OPA_IMAGE,
                [
                    "run",
                    "--server",
                    "--addr=0.0.0.0:8181",
                    "--log-level=error",
                    "--disable-telemetry",
                    "--bundle",
                    "/policy",
                ],
                detach=True,
                ports={"8181/tcp": ("127.0.0.1", None)},
                volumes={str(bundle): {"bind": "/policy", "mode": "ro"}},
                labels={"dios.purpose": "governance-matrix"},
                mem_limit="256m",
            )
            containers.append(opa)
            opa.reload()
            opa_url = (
                "http://127.0.0.1:"
                + opa.attrs["NetworkSettings"]["Ports"]["8181/tcp"][0]["HostPort"]
            )
            with httpx.Client(trust_env=False, timeout=0.2) as http:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if http.get(opa_url + "/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Native OPA not ready")
            pg_image = client.images.get("postgres:16").id
            password = secrets.token_hex(24)
            pg = client.containers.run(
                pg_image,
                detach=True,
                ports={"5432/tcp": ("127.0.0.1", None)},
                environment={
                    "POSTGRES_PASSWORD": password,
                    "POSTGRES_DB": "qualification",
                },
                labels={"dios.purpose": "governance-matrix"},
                mem_limit="512m",
            )
            containers.append(pg)
            pg.reload()
            pg_url = (
                "postgresql+asyncpg://postgres:"
                + password
                + "@127.0.0.1:"
                + pg.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
                + "/qualification"
            )

            async def prepare_postgres():
                deadline = time.monotonic() + 30
                while True:
                    db = Database(pg_url)
                    try:
                        await db.migrate()
                        async with db.engine.connect() as c:
                            settings = {
                                name: (
                                    await c.execute(text("SHOW " + name))
                                ).scalar_one()
                                for name in (
                                    "server_version",
                                    "fsync",
                                    "synchronous_commit",
                                    "full_page_writes",
                                )
                            }
                        assert all(
                            settings[n] == "on"
                            for n in ("fsync", "synchronous_commit", "full_page_writes")
                        )
                        return settings
                    except (OSError, ConnectionError):
                        if time.monotonic() >= deadline:
                            raise
                        await asyncio.sleep(0.2)
                    finally:
                        await db.close()

            settings = asyncio.run(prepare_postgres())

            async def pause(_self):
                await asyncio.to_thread(opa.pause)

            async def resume(_self):
                await asyncio.to_thread(opa.unpause)

            configuration = {
                "kind": "opa",
                "url": opa_url + "/v1/data/dios/tools/allow",
                "bundles": {"/policy": "matrix-v1"},
                "timeout_seconds": 0.5,
            }
            for storage, url in [("sqlite", None), ("postgresql", pg_url)]:
                for provider, config in [("builtin", None), ("opa", configuration)]:
                    name = storage + "-" + provider
                    case = type(
                        name.replace("-", "_"),
                        (GovernanceContract, unittest.IsolatedAsyncioTestCase),
                        {
                            "database_url": url,
                            "evaluator_configuration": config,
                            "observations": observations,
                            "provider_pause": pause,
                            "provider_resume": resume,
                        },
                    )
                    started = time.monotonic()
                    with (output.parent / (name + ".log")).open("w") as log:
                        result = unittest.TextTestRunner(stream=log, verbosity=2).run(
                            unittest.defaultTestLoader.loadTestsFromTestCase(case)
                        )
                    summary = {
                        "storage": storage,
                        "policy": provider,
                        "tests": result.testsRun,
                        "failures": len(result.failures),
                        "errors": len(result.errors),
                        "skipped": len(result.skipped),
                        "seconds": round(time.monotonic() - started, 3),
                    }
                    summaries.append(summary)
                    print(json.dumps(summary), flush=True)
            report = {
                "passed": all(
                    not x["failures"] and not x["errors"] and not x["skipped"]
                    for x in summaries
                ),
                "arms": summaries,
                "observations": observations,
                "postgres": {"image": pg_image, "settings": settings},
                "opa": {
                    "image": opa.image.id,
                    "reference": OPA_IMAGE,
                    "bundle_revision": "matrix-v1",
                },
                "limits": [
                    "Single Linux host; recording tool transport, no new Harness/model run.",
                    "Revocation ordering is the Runtime transactional boundary. Independent upstream policy updates are not atomically committed with Runtime.",
                    "Control restart recreates Runtime connections and receipt service; not a host power-loss experiment.",
                ],
            }
            output.write_text(json.dumps(report, indent=2) + "\n")
            if not report["passed"]:
                raise SystemExit(1)
    finally:
        for container in reversed(containers):
            container.remove(force=True, v=True)
        client.close()


if __name__ == "__main__":
    main()
