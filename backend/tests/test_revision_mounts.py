"""Controlled code/Prompt snapshots and explicitly separate runtime input mounts."""

import copy
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runtime_capacity as fixtures
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import RuntimeErrorCode
from app.runtime.execution_revisions import load_profile, publish_profile
from app.runtime.revision_mounts import build_context
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker
from work_auth_fixtures import policy_fixture


class MountContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "prompt.md").write_text("原始提示词", encoding="utf-8")
        self.raw = {
            "launch": {
                "image": "sha256:" + "1" * 64,
                "mounts": [
                    {
                        "source": str(self.root),
                        "target": "/opt/agent",
                        "read_only": True,
                    }
                ],
            }
        }

    def test_snapshot_is_content_addressed_and_contains_only_explicit_sources(self):
        first, descriptors = build_context(self.raw, {})
        with tarfile.open(fileobj=first) as tar:
            data = tar.extractfile("0/prompt.md").read()
            self.assertEqual(data.decode(), "原始提示词")
            dockerfile = tar.extractfile("Dockerfile").read().decode()
            self.assertIn('RUN ["rm", "-rf", "--", "/opt/agent"]', dockerfile)
        again, same = build_context(self.raw, {})
        self.assertEqual(descriptors, same)
        (self.root / "prompt.md").write_text("新提示词", encoding="utf-8")
        changed, newer = build_context(self.raw, {})
        self.assertNotEqual(descriptors[0]["digest"], newer[0]["digest"])
        for stream in (first, again, changed):
            stream.close()

    def test_links_size_limits_and_hidden_targets_rejected(self):
        (self.root / "link").symlink_to("prompt.md")
        with self.assertRaisesRegex(RuntimeErrorCode, "REVISION_MOUNT_UNSUPPORTED"):
            build_context(self.raw, {})
        (self.root / "link").unlink()
        with (
            patch("app.runtime.revision_mounts.MAX_BYTES", 1),
            self.assertRaisesRegex(RuntimeErrorCode, "REVISION_MOUNT_TOO_LARGE"),
        ):
            build_context(self.raw, {})
        for config in ({"OnBuild": ["RUN echo unsafe"]}, {"Volumes": {"/opt": {}}}):
            with self.assertRaises(RuntimeErrorCode):
                build_context(self.raw, config)
        self.raw["launch"]["mounts"][0]["target"] = "/tmp/agent"
        with self.assertRaisesRegex(RuntimeErrorCode, "REVISION_MOUNT_TARGET_SHADOWED"):
            build_context(self.raw, {})

    def test_input_mounts_are_explicit_and_cannot_hide_packaged_code(self):
        raw = copy.deepcopy(self.raw)
        raw["input_mount_targets"] = ["/data"]
        raw["launch"]["mounts"].append(
            {"source": str(self.root), "target": "/data", "read_only": True}
        )
        context, descriptors = build_context(raw, {})
        self.assertEqual([d["target"] for d in descriptors], ["/opt/agent"])
        context.close()
        raw["input_mount_targets"] = ["/opt/agent/input"]
        raw["launch"]["mounts"][1]["target"] = "/opt/agent/input"
        with self.assertRaisesRegex(RuntimeErrorCode, "REVISION_MOUNT_TARGET_SHADOWED"):
            build_context(raw, {})


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER"), "native Docker opt-in required"
)
class NativeMountRevisionTests(unittest.IsolatedAsyncioTestCase):
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def test_removed_source_still_executes_frozen_code_and_prompt(self):
        import docker
        from app.runtime.docker_adapter import DockerRuntimeAdapter

        client = docker.from_env()
        built = set()
        adapter = DockerRuntimeAdapter(self.scope)
        handles = []
        source = self.root / "code"
        source.mkdir()
        (source / "task.py").write_text(
            "import json,pathlib\n"
            "text=pathlib.Path('/opt/revision-agent/prompt.txt').read_text()\n"
            "print(json.dumps({'type':'session','session_id':text}),flush=True)\n"
            "print(json.dumps({'type':'result','session_id':text,'result':{'summary':text,'evidence':['fixture:packaged-content']}}),flush=True)\n"
        )
        raw = {
            "id": "mount-agent",
            "bridge": "jsonl-v1",
            "governance": {"mode": "e2ag"},
            "launch": {
                "image": os.environ.get(
                    "DIOS_REVISION_TEST_IMAGE",
                    "dios-backend:execution-revisions-20261125",
                ),
                "command": ["python", "/opt/revision-agent/task.py"],
                "mounts": [
                    {
                        "source": str(source),
                        "target": "/opt/revision-agent",
                        "read_only": True,
                    }
                ],
            },
        }
        path = self.root / "profile.json"
        path.write_text(json.dumps(raw))
        try:
            snapshots = []
            for value in ("old-version", "new-version"):
                (source / "prompt.txt").write_text(value)
                row = await publish_profile(
                    self.db, self.scope, path, value, "test-operator"
                )
                ref = row["revision"]["id"]
                _, name, spec, _, _ = await load_profile(self.db, self.scope, ref)
                built.add(spec.image)
                self.assertEqual(spec.mounts, ())
                snapshots.append((ref, name, spec, value))
            for file in source.iterdir():
                file.unlink()
            source.rmdir()
            for ref, name, spec, value in snapshots:
                policy, grant = policy_fixture(self.root, name, spec)
                run = await self.store.submit(
                    value,
                    {
                        "runtime_profile": name,
                        "profile_digest": spec.fingerprint(),
                        "secret_refs": [],
                        "payload": {},
                    },
                    revision_ref=ref,
                    authorization=grant,
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
                self.assertEqual(result["id"], run["id"])
                current = await self.store.get(run["id"])
                handles.append(current["attempt_id"])
                self.assertEqual(result["state"], "succeeded", current)
                self.assertEqual(current["attempts"][0]["session_id"], value)
        finally:
            for handle in handles:
                await adapter.remove(handle)
            for image in built:
                client.images.remove(image)
            client.close()


if __name__ == "__main__":
    unittest.main()
