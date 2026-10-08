"""Disposable real PostgreSQL server; every database is exported before cleanup."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tmp/heterogeneity-deps"))
import asyncpg
from run_file_recovery import save_new

IMAGE = "sha256:108b27c919e6e7bc124350fb265deea9adac58e118eade428c6d1ad44b90debe"
TABLES = {"events": "event_logs", "tasks": "a2a_tasks", "grants": "e2ag_tool_grants",
          "approvals": "e2ag_approvals", "invocations": "e2ag_tool_invocations",
          "receipts": "hetero_receipts", "conflicts": "hetero_conflicts"}


def docker(*args):
    result = subprocess.run(["wsl", "-d", "Ubuntu-24.04", "--", "docker", *args], capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError("Isolated Docker operation failed: " + result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8").strip()


class Postgres:
    def __init__(self, port, password, container):
        self.port, self.password, self.container = port, password, container

    async def connect(self, database="postgres"):
        return await asyncpg.connect(host="127.0.0.1", port=self.port, user="e2ag_admin", password=self.password,
                                     database=database, timeout=5, command_timeout=20)

    async def create(self, name):
        if not name.startswith("e2ag_") or not name.replace("_", "").isalnum():
            raise ValueError("Unexpected experiment database name")
        connection = await self.connect()
        try:
            await connection.execute('CREATE DATABASE "' + name + '"')
        finally:
            await connection.close()
        return f"postgresql+asyncpg://e2ag_admin:{self.password}@127.0.0.1:{self.port}/{name}"

    async def export(self, name, folder):
        connection = await self.connect(name)
        try:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                output = {"backend": "postgresql", "database": name,
                          "version": await connection.fetchval("select version()"), "tables": {}}
                for key, table in TABLES.items():
                    rows = await connection.fetch("select (to_jsonb(t) - 'token_hash')::text as row from " + table + " t")
                    output["tables"][key] = [json.loads(r["row"]) for r in rows]
                output["column_types"] = [dict(r) for r in await connection.fetch(
                    "select table_name,column_name,data_type from information_schema.columns where table_schema='public' order by table_name,ordinal_position")]
                save_new(folder / "postgres-export.json", output)
        finally:
            await connection.close()
        # Server-native plain SQL backup; excludes roles/passwords, includes
        # experimental token hashes (not usable bearer credentials).
        dump = docker("exec", self.container, "pg_dump", "-U", "e2ag_admin", "--no-owner", "--no-privileges", name)
        with (folder / "postgres.sql").open("x", encoding="utf-8") as handle:
            handle.write(dump + "\n")


@contextmanager
def postgres(output):
    keepalive = subprocess.Popen(["wsl", "-d", "Ubuntu-24.04", "--", "cat"], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    password = secrets.token_urlsafe(24)
    name = "e2ag-rr022-pg-20261007-" + uuid.uuid4().hex[:8]
    container = None
    try:
        container = docker("run", "-d", "--name", name, "--memory", "512m", "--cpus", "1",
            "-p", "127.0.0.1::5432", "-e", "POSTGRES_USER=e2ag_admin", "-e", "POSTGRES_PASSWORD=" + password, IMAGE)
        ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", container))
        database = Postgres(int(ports["5432/tcp"][0]["HostPort"]), password, container)
        async def ready():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    connection = await database.connect()
                    try:
                        return await connection.fetchval("select version()")
                    finally:
                        await connection.close()
                except (OSError, asyncpg.PostgresError):
                    await asyncio.sleep(.3)
            raise RuntimeError("PostgreSQL did not become ready")
        version = asyncio.run(ready())
        save_new(output / "postgres-server.json", {"container": name, "container_id": container, "image_id": IMAGE,
            "version": version, "asyncpg_version": asyncpg.__version__, "ports": ports,
            "isolation": "default READ COMMITTED; export REPEATABLE READ READ ONLY; loopback only",
            "storage": "fresh Docker image-declared volume, removed only after per-case SQL dumps"})
        yield database
    finally:
        try:
            if container:
                state = json.loads(docker("inspect", "--format", "{{json .State}}", container))
                save_new(output / "postgres-final-state.json", state)
                docker("stop", "--time", "5", container)
                docker("rm", "-v", container)
                save_new(output / "postgres-cleanup.json", {"container_id": container, "removed": True,
                    "new_anonymous_volume_removed": True})
        finally:
            keepalive.stdin.close()
            keepalive.wait(timeout=15)
