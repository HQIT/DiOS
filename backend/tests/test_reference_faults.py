"""Literal R02/R12 checks over disposable native processes and remote byte APIs."""

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import unittest
import zlib
from dataclasses import replace
from pathlib import Path

import test_runtime_remote_control as remote_fixtures
import test_runtime_worker as worker_fixtures
from app.runtime.adapter import Mount
from app.runtime.capacity import Capacity
from app.runtime.remote_runtime import RemoteRuntimeStore

ROOT = Path(__file__).resolve().parents[2]
OBSERVATIONS = []


class QualificationAccountingTests(unittest.TestCase):
    def test_partial_skipped_and_failed_checks_never_pass_a_scenario(self):
        spec = importlib.util.spec_from_file_location(
            "qualification", ROOT / "scripts/qualify_reference_faults.py"
        )
        qualification = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qualification)

        class SyntheticOutcomes(unittest.TestCase):
            def test_pass(self):
                pass

            def test_subtest_skip(self):
                with self.subTest(part=1):
                    self.skipTest("missing native fixture")

            def test_subtest_fail(self):
                with self.subTest(part=1):
                    self.fail("injected assertion failure")

            @unittest.expectedFailure
            def test_expected_failure(self):
                self.fail("still not supported")

        result = unittest.TextTestRunner(
            stream=io.StringIO(), resultclass=qualification.Result
        ).run(unittest.defaultTestLoader.loadTestsFromTestCase(SyntheticOutcomes))
        by_method = {k.rsplit(".", 1)[-1]: v for k, v in result.outcomes.items()}
        self.assertEqual(
            by_method,
            {
                "test_pass": "passed",
                "test_subtest_skip": "skipped",
                "test_subtest_fail": "failed",
                "test_expected_failure": "blocked",
            },
        )
        status = qualification.scenario_status
        for outcome in ("not_run", "skipped", "blocked"):
            self.assertEqual(
                status({"tests": ["x"], "gaps": []}, {"x": outcome}), "blocked"
            )
        self.assertEqual(status({"tests": [], "gaps": []}, {}), "blocked")
        self.assertEqual(
            status({"tests": ["x"], "gaps": ["unmet"]}, {"x": "passed"}), "blocked"
        )
        self.assertEqual(
            status({"tests": ["x"], "gaps": ["unmet"]}, {"x": "failed"}), "failed"
        )
        self.assertEqual(
            status({"tests": ["x"], "gaps": []}, {"x": "passed"}), "passed"
        )


def review_input():
    source = ROOT / "docs/design/fixtures/reference-scenarios.json"
    return next(
        x for x in json.loads(source.read_text())["scenarios"] if x["id"] == "review"
    )


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1", "isolated native Docker required"
)
class NativeReferenceFaults(unittest.IsolatedAsyncioTestCase):
    async def test_three_live_workers_then_one_stops(self):
        f = worker_fixtures.DockerWorkerTests()
        await f.asyncSetUp()
        self.addAsyncCleanup(f.asyncTearDown)
        f.barrier = f.root / "barrier"
        f.barrier.mkdir(mode=0o755)
        f.spec = replace(f.spec, mounts=(Mount(str(f.barrier), "/run/fixture", True),))
        await Capacity(f.store).put("reference-limit", {"kind": "scope"}, 3, 0)
        digest = hashlib.sha256(
            json.dumps(review_input(), sort_keys=True).encode()
        ).hexdigest()
        runs = [
            await f.submit(
                str(i), delay=0.1, barrier=True, reference_input_digest=digest
            )
            for i in range(3)
        ]
        children = [await f.child() for _ in range(3)]
        first = await f.wait_state(lambda s: s["actual_concurrency"] == 3)
        self.assertEqual(len({w["pid"] for w in first["workers"]}), 3)
        self.assertEqual(len({a["process_id"] for a in first["attempts"]}), 3)
        self.assertEqual(len({a["session_id"] for a in first["attempts"]}), 3)
        evidence = []
        for run in runs:
            a = (await f.store.get(run["id"]))["attempts"][0]
            boot = a["remote"]["launch"]["worker_boot_id"]
            self.assertEqual(
                boot, Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            )
            evidence.append(
                {
                    k: a[k]
                    for k in (
                        "id",
                        "worker_id",
                        "worker_instance",
                        "process_id",
                        "process_instance",
                        "session_id",
                        "epoch",
                    )
                }
                | {"run_id": run["id"], "boot_id": boot}
            )
        stopped = first["attempts"][0]["attempt_id"]
        await f.adapter.stop(stopped)
        second = await f.wait_state(lambda s: s["actual_concurrency"] == 2)
        self.assertEqual(second["registered_slots"], 3)
        (f.barrier / "release").touch()
        for child in children:
            _, err = await asyncio.wait_for(child.communicate(), 20)
            self.assertEqual(child.returncode, 0, err)
        states = [(await f.store.get(r["id"]))["state"] for r in runs]
        self.assertEqual(sorted(states), ["failed", "succeeded", "succeeded"])
        OBSERVATIONS.append(
            {
                "case": "R02",
                "input_digest": digest,
                "configured_limit": 3,
                "before": first["actual_concurrency"],
                "after": second["actual_concurrency"],
                "workers": [
                    {k: w[k] for k in ("id", "pid", "instance")}
                    for w in first["workers"]
                ],
                "attempts": evidence,
                "states": states,
            }
        )


class ByteReferenceFaults(unittest.IsolatedAsyncioTestCase):
    async def test_exact_chunk_sizes_resume_after_lost_ack(self):
        data = review_input()["input"]["utf8_sample"].encode("utf-8")
        raw_sha = hashlib.sha256(data).hexdigest()
        compressed = zlib.compress(data)
        compressed_sha = hashlib.sha256(compressed).hexdigest()
        self.assertNotEqual(raw_sha, compressed_sha)
        self.assertEqual(zlib.decompress(compressed), data)
        for width in (1, 2, 3, 7):
            with self.subTest(width=width):
                f = remote_fixtures.RemoteControlTests()
                await f.asyncSetUp()
                try:
                    attempt, binding = await f.claim()
                    remote = f.remote
                    await remote.bound(
                        "upload.begin", binding, key=raw_sha, size=len(data)
                    )
                    original = remote.transport.exchange
                    lost = False

                    async def exchange(envelope, original=original):
                        nonlocal lost
                        value = await original(envelope)
                        if (
                            envelope["message"]["method"] == "runtime.upload.chunk"
                            and not lost
                        ):
                            lost = True
                            raise ConnectionError("synthetic lost acknowledgement")
                        return value

                    remote.transport.exchange = exchange
                    with self.assertRaises(ConnectionError):
                        await remote.bound(
                            "upload.chunk",
                            binding,
                            key=raw_sha,
                            offset=0,
                            data=base64.b64encode(data[:width]).decode(),
                        )
                    remote.transport.exchange = original
                    # New client object resumes the same authenticated Worker binding.
                    resumed = RemoteRuntimeStore(f.f.scope, remote.transport)
                    resumed.instance = remote.instance
                    state = await resumed.bound(
                        "upload.begin", binding, key=raw_sha, size=len(data)
                    )
                    self.assertEqual(state["received"], width)
                    offset = state["received"]
                    while offset < len(data):
                        part = data[offset : offset + width]
                        receipt = await resumed.bound(
                            "upload.chunk",
                            binding,
                            key=raw_sha,
                            offset=offset,
                            data=base64.b64encode(part).decode(),
                        )
                        offset += len(part)
                        self.assertEqual(receipt["received"], offset)
                    await resumed.bound("upload.finish", binding, key=raw_sha)
                    received = await resumed.bound(
                        "artifact.read", binding, key=raw_sha, offset=0
                    )
                    raw = base64.b64decode(received["data"])
                    self.assertEqual(raw, data)
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), raw_sha)
                    self.assertEqual(
                        raw.decode(), review_input()["input"]["utf8_sample"]
                    )
                    OBSERVATIONS.append(
                        {
                            "case": "R12",
                            "width": width,
                            "resumed_offset": width,
                            "run_id": f.run["id"],
                            "attempt_id": attempt["id"],
                            "epoch": attempt["epoch"],
                            "bytes": len(raw),
                            "raw_sha256": raw_sha,
                            "compressed_sha256": compressed_sha,
                            "encoding": "raw UTF-8 bytes over base64; zlib digest independent",
                        }
                    )
                finally:
                    await f.f.asyncTearDown()
