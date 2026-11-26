"""Isolated PostgreSQL roles/TLS and real mTLS Worker qualification; no model calls."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database
from app.runtime.schema import artifacts
from app.runtime.store import RuntimeStore
from app.runtime.work_auth_schema import grants
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_remote_recovery_qualification import (
    RemoteRecoveryQualificationTests,
)

CONFIG = {}
OBSERVATIONS = {}


def private(path, data):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(data)


def command(*args, **kwargs):
    result = subprocess.run(
        args, capture_output=True, timeout=60, check=False, **kwargs
    )
    if result.returncode:
        # Credentials and provider output stay in the private qualification directory.
        with (CONFIG["root"] / "errors.log").open("ab") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(result.stderr)
        raise RuntimeError("QUALIFICATION_COMMAND_FAILED:" + args[0])
    return result.stdout


def prepare(root, image):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    CONFIG["root"] = root
    name = "dios-shared-" + uuid4().hex[:10]
    CONFIG["container"] = name
    image_id = (
        command("docker", "image", "inspect", image, "--format", "{{.Id}}")
        .decode()
        .strip()
    )
    CONFIG["postgres_image"] = image_id
    for role in ("owner", "runtime", "backup"):
        CONFIG[role + "_password"] = secrets.token_hex(24)
    private(
        root / "postgres.env",
        "POSTGRES_DB=dios\nPOSTGRES_PASSWORD=" + secrets.token_hex(24) + "\n",
    )
    private(
        root / "roles.env",
        "".join(
            "DIOS_PG_" + role.upper() + "_PASSWORD=" + CONFIG[role + "_password"] + "\n"
            for role in ("owner", "runtime", "backup")
        ),
    )
    private(
        root / "installation.json",
        json.dumps({"container": name, "volume": name, "image": image_id}),
    )
    command(
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--env-file",
        str(root / "postgres.env"),
        "--mount",
        "type=volume,source=" + name + ",target=/var/lib/postgresql/data",
        "-p",
        "127.0.0.1::5432",
        image_id,
    )
    wait_database(name)
    for args in (
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "pg-ca.key",
            "-out",
            "pg-ca.pem",
            "-subj",
            "/CN=DiOS qualification CA",
            "-days",
            "2",
        ],
        [
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "pg-server.key",
            "-out",
            "pg-server.csr",
            "-subj",
            "/CN=localhost",
        ],
    ):
        command("openssl", *args, cwd=root)
    private(
        root / "pg-server.ext",
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n",
    )
    command(
        "openssl",
        "x509",
        "-req",
        "-in",
        "pg-server.csr",
        "-CA",
        "pg-ca.pem",
        "-CAkey",
        "pg-ca.key",
        "-CAcreateserial",
        "-out",
        "pg-server.pem",
        "-days",
        "2",
        "-extfile",
        "pg-server.ext",
        cwd=root,
    )
    private(
        root / "pg_hba.conf",
        "local all all trust\nhostssl all all 0.0.0.0/0 scram-sha-256\nhostssl all all ::/0 scram-sha-256\nhostnossl all all 0.0.0.0/0 reject\nhostnossl all all ::/0 reject\n",
    )
    for source, target in (
        ("pg-server.pem", "server.crt"),
        ("pg-server.key", "server.key"),
        ("pg_hba.conf", "pg_hba.conf"),
    ):
        command(
            "docker",
            "cp",
            str(root / source),
            name + ":/var/lib/postgresql/data/" + target,
        )
        command(
            "docker",
            "exec",
            "-u",
            "root",
            name,
            "chown",
            "postgres:postgres",
            "/var/lib/postgresql/data/" + target,
        )
    command(
        "docker",
        "exec",
        "-u",
        "root",
        name,
        "chmod",
        "600",
        "/var/lib/postgresql/data/server.key",
    )
    for statement in (
        "ALTER SYSTEM SET ssl = 'on'",
        "ALTER SYSTEM SET ssl_cert_file = 'server.crt'",
        "ALTER SYSTEM SET ssl_key_file = 'server.key'",
    ):
        command(
            "docker",
            "exec",
            name,
            "psql",
            "-U",
            "postgres",
            "-d",
            "dios",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            statement,
        )
    command("docker", "restart", name)
    wait_database(name)
    port = json.loads(
        command(
            "docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", name
        )
    )["5432/tcp"][0]["HostPort"]
    command(
        "docker",
        "cp",
        str(ROOT / "deploy/shared/roles.sql"),
        name + ":/tmp/dios-roles.sql",
    )
    command(
        "docker",
        "exec",
        "--env-file",
        str(root / "roles.env"),
        name,
        "psql",
        "-X",
        "-U",
        "postgres",
        "-d",
        "dios",
        "-f",
        "/tmp/dios-roles.sql",
    )
    os.environ["PGSSLROOTCERT"] = str(root / "pg-ca.pem")
    for role in ("owner", "runtime", "backup"):
        CONFIG[role + "_url"] = (
            "postgresql+asyncpg://dios_"
            + role
            + ":"
            + CONFIG[role + "_password"]
            + "@localhost:"
            + port
            + "/dios?ssl=verify-full"
        )
    private(
        root / "connections.json",
        json.dumps({k: v for k, v in CONFIG.items() if k.endswith("_url")}),
    )


def wait_database(name):
    for _ in range(100):
        ready = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "dios"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if ready.returncode == 0:
            return
        time.sleep(0.1)
    raise RuntimeError("POSTGRES_START_TIMEOUT")


class SharedFixture:
    async def asyncSetUp(self):
        self.root = CONFIG["root"] / "runtime"
        self.root.mkdir()
        self.db = Database(CONFIG["runtime_url"])
        self.scope = "shared-" + uuid4().hex
        self.store = RuntimeStore(self.db, self.scope)
        self.provider = LocalArtifacts(self.root / "artifacts")

    async def asyncTearDown(self):
        await self.db.close()


class SharedQualification(RemoteRecoveryQualificationTests):
    fixture_type = SharedFixture

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Replace the in-process fixture endpoint with the shipped CLI gateway.
        port = self.gateway.server.sockets[0].getsockname()[1]
        self.stop.set()
        await self.gateway_task
        self.gateway_task = None
        config = {
            "version": 1,
            "enabled": True,
            "host": "127.0.0.1",
            "port": port,
            "audience": "qualification",
            "runtime_control_enabled": True,
            "ca_file": str(self.root / "ca.pem"),
            "certificate_file": str(self.root / "server.pem"),
            "key_file": str(self.root / "server.key"),
        }
        private(self.root / "gateway.json", json.dumps(config))
        private(self.root / "tools.json", json.dumps({"version": 1, "tools": {}}))
        self.gateway_log = (self.root / "gateway.log").open("wb")
        os.chmod(self.gateway_log.name, 0o600)
        self.addCleanup(self.gateway_log.close)
        env = dict(
            os.environ,
            PYTHONPATH=str(ROOT / "backend"),
            DIOS_RUNTIME_DATABASE_URL=CONFIG["runtime_url"],
        )
        self.gateway_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.runtime.cli",
            "--scope",
            self.store.scope,
            "tool-gateway",
            "--config",
            str(self.root / "gateway.json"),
            "--artifacts",
            str(self.fixture.provider.root),
            "--policy-file",
            str(self.policy.path),
            "--tools-file",
            str(self.root / "tools.json"),
            "--identities-file",
            str(self.root / "machines.json"),
            "--profiles-file",
            str(self.root / "profiles.json"),
            cwd=ROOT / "backend",
            env=env,
            stdout=self.gateway_log,
            stderr=self.gateway_log,
        )
        self.addAsyncCleanup(self.stop_cli_gateway)
        async with asyncio.timeout(15):
            while True:
                if self.gateway_process.returncode is not None:
                    raise RuntimeError("CLI_GATEWAY_EXITED")
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.1)

    async def stop_cli_gateway(self):
        if self.gateway_process.returncode is None:
            self.gateway_process.terminate()
        await asyncio.wait_for(self.gateway_process.wait(), 15)

    async def test_shared_state_objects_roles_and_tls(self):
        wrong_host = Database(
            CONFIG["runtime_url"].replace("@localhost:", "@127.0.0.1:")
        )
        try:
            with self.assertRaisesRegex(Exception, "certificate|IP address mismatch"):
                async with wrong_host.engine.connect():
                    self.fail("wrong TLS hostname accepted")
        finally:
            await wrong_host.close()
        async with self.fixture.db.read(self.store.scope) as conn:
            self.assertTrue(
                (
                    await conn.execute(
                        text("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()")
                    )
                ).scalar()
            )
            self.assertEqual(
                (await conn.execute(text("SELECT current_user"))).scalar(),
                "dios_runtime",
            )
        with self.assertRaises(DBAPIError):
            async with self.fixture.db.engine.begin() as conn:
                await conn.execute(text("CREATE TABLE forbidden_ddl (id integer)"))
        backup = Database(CONFIG["backup_url"])
        self.addAsyncCleanup(backup.close)
        with self.assertRaises(DBAPIError):
            async with backup.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE runtime_runs SET version=version WHERE false")
                )
        rows = [await self.submit("shared-" + str(n)) for n in range(2)]
        duplicate = await self.submit("shared-0")
        self.assertEqual(duplicate["id"], rows[0]["id"])
        children = [await self.launch() for _ in rows]
        observed = await self.concurrency(2)
        (self.control / "release").touch()
        results = await asyncio.gather(*(self.collect(child) for child in children))
        self.assertEqual([r["state"] for r in results], ["succeeded", "succeeded"])
        async with backup.read(self.store.scope) as conn:
            linked = (
                (
                    await conn.execute(
                        select(artifacts).where(
                            artifacts.c.run_id.in_([r["id"] for r in rows])
                        )
                    )
                )
                .mappings()
                .all()
            )
            authorized = (
                (
                    await conn.execute(
                        select(grants).where(
                            grants.c.run_id.in_([r["id"] for r in rows])
                        )
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual(len(authorized), 2)
        # Native fixture stderr is empty; each Attempt retains stdout and result.
        self.assertEqual(len(linked), 4)
        self.assertTrue(
            all(sum(row["run_id"] == run["id"] for row in linked) == 2 for run in rows)
        )
        self.assertTrue(
            all(self.fixture.provider.verify(row["storage_key"]) for row in linked)
        )
        for row in linked:
            self.assertEqual(
                hashlib.sha256(
                    b"".join(self.fixture.provider.read(row["storage_key"]))
                ).hexdigest(),
                row["digest"],
            )
        OBSERVATIONS.update(
            {
                "scope": self.store.scope,
                "postgres_tls": True,
                "wrong_tls_hostname_rejected": True,
                "runtime_ddl_denied": True,
                "backup_mutation_denied": True,
                "idempotent_submit": True,
                "remote_gateway_pid": self.gateway_process.pid,
                "worker_database_credentials": False,
                "native": observed,
                "runs": [
                    {
                        "id": r["id"],
                        "state": r["state"],
                        "attempt_id": r["attempts"][0]["id"],
                    }
                    for r in results
                ],
                "verified_artifact_references": len(linked),
                "e2ag_grants": len(authorized),
            }
        )


async def migrate():
    db = Database(CONFIG["owner_url"])
    try:
        await db.migrate()
    finally:
        await db.close()


def backup_dump():
    root, name = CONFIG["root"], CONFIG["container"]
    command("docker", "cp", str(root / "pg-ca.pem"), name + ":/tmp/pg-ca.pem")
    private(
        root / "backup.env",
        "PGPASSWORD="
        + CONFIG["backup_password"]
        + "\nPGSSLMODE=verify-full\nPGSSLROOTCERT=/tmp/pg-ca.pem\n",
    )
    data = command(
        "docker",
        "exec",
        "--env-file",
        str(root / "backup.env"),
        name,
        "pg_dump",
        "-h",
        "localhost",
        "-U",
        "dios_backup",
        "-d",
        "dios",
        "--format=custom",
        "--no-owner",
        "--no-acl",
    )
    if not data.startswith(b"PGDMP"):
        raise RuntimeError("BACKUP_FORMAT_INVALID")
    target = root / "runtime.dump"
    with target.open("xb") as stream:
        os.chmod(target, 0o600)
        stream.write(data)
    return {
        "role": "dios_backup",
        "format": "pg_dump/custom",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "clean_restore_qualified": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, required=True, help="new private directory"
    )
    parser.add_argument("--postgres-image", default="postgres:16-alpine")
    args = parser.parse_args()
    prepare(args.output.resolve(), args.postgres_image)
    asyncio.run(migrate())
    os.environ["DIOS_TEST_DOCKER"] = "1"
    # The inherited class decorator reads this when imported; opt-in was explicit.
    SharedQualification.__unittest_skip__ = False
    suite = unittest.TestSuite(
        [SharedQualification("test_shared_state_objects_roles_and_tls")]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful() and not result.skipped:
        OBSERVATIONS["database_backup"] = backup_dump()
    report = {
        "work_item": "D01-2.01",
        "passed": result.wasSuccessful() and not result.skipped,
        "tests": result.testsRun,
        "postgres_image": CONFIG["postgres_image"],
        "observations": OBSERVATIONS,
        "limits": [
            "One Linux host; independent CLI gateway and two remote Worker processes.",
            "Single authoritative POSIX artifact owner; no RWX filesystem or S3 adapter qualification.",
            "Data migration, clean restore and physical multi-host failover retain separate WBS acceptance.",
        ],
    }
    (args.output / "evidence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps({"passed": report["passed"], "tests": result.testsRun}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
