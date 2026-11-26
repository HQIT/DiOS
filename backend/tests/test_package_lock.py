"""Resolver conflicts, byte integrity, deployment gate and frozen native execution."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runtime_capacity as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.execution_revisions import (
    compile_profile,
    load_profile,
    publish_profile,
)
from app.runtime.package_lock import manifest, resolve, verify
from app.runtime.package_payloads import TARGET, materialize, tree_descriptor
from app.runtime.revisions import Revisions
from app.runtime.store import digest
from test_execution_revisions import profile

OBSERVATIONS = []


def requirement(name, spec=""):
    return {"name": name, "specifier": spec}


def package(
    name="agent", version="1.0", dependencies=(), content=b"original", path="data.txt"
):
    return {
        "schema_version": 1,
        "name": name,
        "version": version,
        "kind": "harness",
        "dependencies": list(dependencies),
        "files": [
            {
                "path": path,
                "mediaType": "application/octet-stream",
                "size": len(content),
                "mode": 0o644,
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        ],
    }


def seal(value):
    value["digest"] = "sha256:" + digest(
        {k: v for k, v in value.items() if k != "digest"}
    )
    return value


class PackageLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = resolve([package()], [requirement("agent")])

    def test_backtracking_selects_compatible_transitive_version(self):
        catalog = [
            package("a", "2", [requirement("shared", ">=2")]),
            package("a", "1", [requirement("shared", "<2")]),
            package("b", "1", [requirement("shared", "<2")]),
            package("shared", "1"),
            package("shared", "2"),
        ]
        result = resolve(catalog, [requirement("a"), requirement("b")])
        self.assertEqual(
            {
                x["manifest"]["name"]: x["manifest"]["version"]
                for x in result["packages"]
            },
            {"a": "1", "b": "1", "shared": "1"},
        )
        self.assertEqual(
            result,
            resolve(list(reversed(catalog)), [requirement("b"), requirement("a")]),
        )

    def test_missing_incompatible_and_cycles_rejected(self):
        for catalog, roots, error in [
            ([], [requirement("missing")], "UNSATISFIED"),
            ([package()], [requirement("agent", ">=2")], "UNSATISFIED"),
            (
                [
                    package("a", dependencies=[requirement("b")]),
                    package("b", dependencies=[requirement("a")]),
                ],
                [requirement("a")],
                "CYCLE",
            ),
        ]:
            with (
                self.subTest(error=error),
                self.assertRaisesRegex(RuntimeErrorCode, error),
            ):
                resolve(catalog, roots)

    def test_same_version_ambiguity_and_non_pep_versions_rejected(self):
        with self.assertRaisesRegex(RuntimeErrorCode, "AMBIGUOUS"):
            resolve(
                [package(version="1.0"), package(version="1.0.0")],
                [requirement("agent")],
            )
        for value in ("latest", "1.0^", ""):
            with self.subTest(value=value), self.assertRaises(RuntimeErrorCode):
                manifest(package(version=value))
        with self.assertRaises(RuntimeErrorCode):
            resolve([package()], [requirement("agent", "^1")])

    def test_digest_changes_and_rehashed_invalid_graph_fail(self):
        changed = copy.deepcopy(self.lock)
        changed["packages"][0]["manifest"]["version"] = "2"
        with self.assertRaisesRegex(RuntimeErrorCode, "LOCK_DIGEST"):
            verify(changed)
        with self.assertRaisesRegex(RuntimeErrorCode, "PACKAGE_DIGEST"):
            verify(seal(changed))
        changed = copy.deepcopy(self.lock)
        changed["roots"][0]["specifier"] = ">=2"
        with self.assertRaisesRegex(RuntimeErrorCode, "UNSATISFIED"):
            verify(seal(changed))
        changed = copy.deepcopy(self.lock)
        changed["packages"].extend(
            resolve([package("extra")], [requirement("extra")])["packages"]
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "UNREACHABLE"):
            verify(seal(changed))

    def test_unsafe_paths_and_file_directory_collisions_fail(self):
        for path in ("../escape", "/absolute", "a/../b", "a//b", "a\\b", ".", "a\nb"):
            with self.subTest(path=path), self.assertRaises(RuntimeErrorCode):
                manifest(package(path=path))
        value = package(path="a")
        value["files"].append(dict(value["files"][0], path="a/b"))
        with self.assertRaisesRegex(RuntimeErrorCode, "PATH"):
            manifest(value)

    def test_payload_bytes_size_and_symlinks_fail(self):
        blobs = self.root / "blobs"
        blobs.mkdir()
        file = (
            blobs
            / self.lock["packages"][0]["manifest"]["files"][0]["digest"].split(":")[1]
        )
        for content in (b"changed!", b"x"):
            file.write_bytes(content)
            with (
                self.subTest(content=content),
                self.assertRaisesRegex(RuntimeErrorCode, "MISMATCH"),
            ):
                materialize(self.lock, blobs, self.root / "out")
        file.unlink()
        real = self.root / "real"
        real.write_bytes(b"original")
        file.symlink_to(real)
        with self.assertRaisesRegex(RuntimeErrorCode, "UNAVAILABLE"):
            materialize(self.lock, blobs, self.root / "out")
        file.unlink()
        with self.assertRaisesRegex(RuntimeErrorCode, "UNAVAILABLE"):
            materialize(self.lock, blobs, self.root / "out")

    def test_materialized_tree_matches_existing_mount_digest(self):
        from app.runtime.revision_mounts import build_context

        blobs = self.root / "blobs"
        blobs.mkdir()
        (blobs / hashlib.sha256(b"original").hexdigest()).write_bytes(b"original")
        output = self.root / "out"
        materialize(self.lock, blobs, output)
        raw = profile()
        raw["launch"]["mounts"] = [
            {"source": str(output), "target": TARGET, "read_only": True}
        ]
        context, descriptors = build_context(raw, {})
        context.close()
        self.assertEqual(descriptors, [tree_descriptor(self.lock)])

    def test_lock_requires_execution_content_binding(self):
        from app.runtime.worker import read_profile

        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FROZEN"):
            read_profile(
                "unused", value=dict(profile(), package_source={"lock": "latest"})
            )
        raw = profile()
        raw["package_lock"] = self.lock
        with self.assertRaisesRegex(RuntimeErrorCode, "BINDING"):
            compile_profile(raw)
        raw["packaged_mounts"] = [tree_descriptor(self.lock)]
        self.assertEqual(
            compile_profile(raw)["dependencies"]["harness"][0]["name"], "package-agent"
        )
        raw["launch"]["mounts"] = [
            {"source": "/tmp", "target": "/opt/dios", "read_only": True}
        ]
        with self.assertRaisesRegex(RuntimeErrorCode, "BINDING"):
            compile_profile(raw)

    def test_cli_resolves_and_verifies_without_database(self):
        catalog, roots, lock = [
            self.root / p for p in ("catalog.json", "roots.json", "lock.json")
        ]
        catalog.write_text(json.dumps([package()]))
        roots.write_text(json.dumps([requirement("agent")]))
        env = {k: v for k, v in os.environ.items() if k != "DIOS_RUNTIME_DATABASE_URL"}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.runtime.cli",
                "package",
                "lock",
                "--catalog",
                str(catalog),
                "--roots",
                str(roots),
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        lock.write_text(result.stdout)
        verified = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.runtime.cli",
                "package",
                "verify",
                "--lock",
                str(lock),
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        self.assertEqual(json.loads(verified.stdout), self.lock)


class PackagePublishGateTests(unittest.IsolatedAsyncioTestCase):
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def test_invalid_bytes_fail_before_docker_or_revision_creation(self):
        lock = resolve([package()], [requirement("agent")])
        (self.root / "lock.json").write_text(json.dumps(lock))
        (self.root / "blobs").mkdir()
        raw = profile()
        raw["package_source"] = {"lock": "lock.json", "blobs": "blobs"}
        path = self.root / "profile.json"
        path.write_text(json.dumps(raw))
        with (
            patch("docker.from_env") as client,
            self.assertRaisesRegex(RuntimeErrorCode, "PAYLOAD_UNAVAILABLE"),
        ):
            await publish_profile(self.db, self.scope, path, "bad", "operator")
        client.assert_not_called()
        self.assertEqual(
            (await Revisions(self.db, self.scope).list(raw["id"]))["items"], []
        )

    async def test_direct_revision_cannot_bypass_deployment_lock_validation(self):
        from app.runtime.deployments import Deployments

        for reason in ("digest", "missing", "incompatible", "cycle"):
            value = compile_profile(profile(mode="e2ag"))
            lock = resolve([package()], [requirement("agent")])
            if reason == "missing":
                lock["roots"] = [requirement("absent")]
            if reason == "incompatible":
                lock["roots"][0]["specifier"] = ">2"
            if reason == "cycle":
                row = lock["packages"][0]
                row["manifest"]["dependencies"] = [requirement("agent")]
                row["digest"] = "sha256:" + digest(row["manifest"])
            seal(lock)
            if reason == "digest":
                lock["digest"] = "sha256:" + "0" * 64
            value["configuration"]["execution_profile"]["package_lock"] = lock
            saved = await Revisions(self.db, self.scope).create(
                reason, value, actor="operator"
            )
            error = {
                "digest": "LOCK_DIGEST",
                "missing": "UNSATISFIED",
                "incompatible": "UNSATISFIED",
                "cycle": "CYCLE",
            }[reason]
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(RuntimeErrorCode, error),
            ):
                await Deployments(self.db, self.scope).request(
                    "deployment",
                    saved["revision"]["id"],
                    reason,
                    0,
                    "operator",
                    {"payload": {}},
                )


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER"), "native Docker opt-in required"
)
class NativePackageTests(unittest.IsolatedAsyncioTestCase):
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def test_native_frozen_dependencies_and_live_e2ag(self):
        import docker
        from app.runtime.artifacts import LocalArtifacts
        from app.runtime.docker_adapter import DockerRuntimeAdapter
        from app.runtime.work_authorizations import WorkAuthorizationVerifier
        from app.runtime.worker import Worker
        from work_auth_fixtures import policy_fixture

        code = (
            b"import json,pathlib\n"
            b"text=pathlib.Path('/opt/dios/packages/prompt/data.txt').read_text()\n"
            b"print(json.dumps({'type':'session','session_id':text}),flush=True)\n"
            b"print(json.dumps({'type':'result','session_id':text,'result':{'summary':text,'evidence':['fixture:locked-packages']}}),flush=True)\n"
        )
        blobs = self.root / "blobs"
        blobs.mkdir()
        (blobs / hashlib.sha256(code).hexdigest()).write_bytes(code)
        raw = profile(mode="e2ag")
        raw["launch"].update(
            image="dios-backend:principal-updates-20261126",
            command=["python", TARGET + "/agent/task.py"],
        )
        raw["package_source"] = {"lock": "lock.json", "blobs": "blobs"}
        path = self.root / "profile.json"
        path.write_text(json.dumps(raw))
        client = docker.from_env()
        adapter = DockerRuntimeAdapter(self.scope)
        images, handles, snapshots = set(), [], []
        try:
            for version, text in (("1", "原始版本"), ("2", "升级版本")):
                content = text.encode()
                (blobs / hashlib.sha256(content).hexdigest()).write_bytes(content)
                agent = package(
                    content=code,
                    path="task.py",
                    dependencies=[requirement("prompt", "==" + version)],
                )
                prompt = package("prompt", version, content=content)
                prompt["kind"] = "prompt"
                lock = resolve([agent, prompt], [requirement("agent")])
                (self.root / "lock.json").write_text(json.dumps(lock))
                saved = await publish_profile(
                    self.db, self.scope, path, version, "operator"
                )
                ref = saved["revision"]["id"]
                frozen, name, spec, _, _ = await load_profile(self.db, self.scope, ref)
                images.add(spec.image)
                self.assertFalse(spec.mounts)
                self.assertNotIn("package_source", frozen)
                snapshots.append((ref, name, spec, text))
            for file in blobs.iterdir():
                file.write_bytes(b"changed source")
            for ref, name, spec, text in snapshots:
                policy, grant = policy_fixture(self.root, name, spec)
                input_value = {
                    "runtime_profile": name,
                    "profile_digest": spec.fingerprint(),
                    "secret_refs": [],
                    "payload": {},
                }
                with self.assertRaisesRegex(
                    RuntimeErrorCode, "WORK_AUTHORIZATION_REQUIRED"
                ):
                    await self.store.submit(
                        "no-grant-" + text, input_value, revision_ref=ref
                    )
                run = await self.store.submit(
                    text, input_value, revision_ref=ref, authorization=grant
                )
                worker = Worker(
                    self.store,
                    adapter,
                    name,
                    spec,
                    LocalArtifacts(self.root / "artifacts"),
                    revision_ref=ref,
                    authorization_verifier=WorkAuthorizationVerifier(
                        self.store, policy
                    ),
                    poll_interval=0.05,
                )
                result = await worker.once()
                current = await self.store.get(run["id"])
                handles.append(current["attempt_id"])
                self.assertEqual(result["state"], "succeeded", current)
                self.assertEqual(current["attempts"][0]["session_id"], text)
                OBSERVATIONS.append(
                    {
                        "revision": ref,
                        "image": spec.image,
                        "expected": text,
                        "session": current["attempts"][0]["session_id"],
                        "state": result["state"],
                    }
                )
        finally:
            for handle in handles:
                await adapter.remove(handle)
            for image in images:
                client.images.remove(image)
            client.close()
