"""R05: reset an explicitly configured isolated Linux VM with native remote Workers."""

import asyncio
import copy
import hashlib
import json
import os
import shlex
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import test_remote_recovery_qualification as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.worker import read_profile

OBSERVATIONS = []


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_REBOOT_CONFIG"),
    "isolated reset-authorized Linux VM required",
)
class NativeNodeRebootTests(unittest.IsolatedAsyncioTestCase):
    async def command(self, *args, timeout=45):
        def run():
            return subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=True
            ).stdout

        return await asyncio.to_thread(run)

    async def ssh(self, command, timeout=45):
        return await self.command(
            "ssh", *self.options, self.target, command, timeout=timeout
        )

    async def wait_run(self, run, predicate, timeout=130):
        async with asyncio.timeout(timeout):
            while True:
                value = await self.f.store.get(run["id"])
                if predicate(value):
                    return value
                await asyncio.sleep(0.2)

    async def asyncSetUp(self):
        self.config = json.loads(
            Path(os.environ["DIOS_TEST_REBOOT_CONFIG"]).read_text()
        )
        c = self.config
        self.assertIs(c["allow_reset"], True)
        self.assertTrue(c["domain"].startswith(("dios-ha-", "dios-test-")))
        self.assertEqual(
            (await self.command("virsh", "domuuid", c["domain"])).strip(),
            c["domain_uuid"],
        )
        self.assertEqual(
            (await self.command("virsh", "domstate", c["domain"])).strip(), "running"
        )
        self.options = [
            "-i",
            c["ssh_key"],
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + c["known_hosts"],
            "-o",
            "ConnectTimeout=4",
        ]
        self.target = c["ssh_user"] + "@" + c["address"]
        self.assertEqual((await self.ssh("sudo docker ps -q")).strip(), "")
        for service in ("pacemaker", "corosync", "dios-control"):
            self.assertIn(
                (await self.ssh("systemctl is-active " + service + " || true")).strip(),
                ("inactive", "failed", "unknown"),
            )
        self.assertEqual(
            (await self.ssh("mountpoint -q /srv/dios-ha; echo $?")).strip(), "32"
        )
        self.output = Path(c["output"]) / ("run-" + uuid4().hex[:8])
        self.output.mkdir(mode=0o700)
        self.guest = "/home/" + c["ssh_user"] + "/dios-r05-" + uuid4().hex[:8]
        self.names = []
        self.units = []
        self.release_complete = asyncio.Event()
        self.complete_waiting = asyncio.Event()
        self.hold_run = None
        self.messages = []
        self.runs = {}
        self.f = fixtures.RemoteRecoveryQualificationTests()
        self.f.gateway_host = c["gateway_host"]
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.fixture.asyncTearDown)
        self.addAsyncCleanup(self.f.cleanup_native)
        self.addAsyncCleanup(self.cleanup_guest)
        self.profile_path = self.f.profiles["account-a"][0]
        profile = json.loads(self.profile_path.read_text())
        profile["launch"]["mounts"][0]["source"] = self.guest + "/control"
        profile["launch"]["image"] = c["fixture_image"]
        # Longer than the real server lease and guest boot; all inputs are synthetic.
        profile["launch"]["command"] = [
            "python",
            "-u",
            "-c",
            (
                "import json,os,pathlib,time,uuid\n"
                "p=json.loads(os.environ['DIOS_WORK_INPUT']);s=str(uuid.uuid4())\n"
                "print(json.dumps({'type':'session','session_id':s}),flush=True)\n"
                "print(json.dumps({'type':'output','text':'节点重启合成任务🙂'}),flush=True)\n"
                "deadline=time.monotonic()+240\n"
                "while p.get('barrier') and not pathlib.Path('/run/fixture/release').exists():\n"
                " if time.monotonic()>deadline:raise RuntimeError('synthetic barrier timeout')\n"
                " time.sleep(.1)\n"
                "print(json.dumps({'type':'result','session_id':s,'result':{'summary':'synthetic reboot result','evidence':['fixture:'+os.environ['DIOS_ATTEMPT_ID']]}}),flush=True)\n"
            ),
        ]
        self.profile_path.write_text(json.dumps(profile))
        _, spec = read_profile(self.profile_path)
        self.f.profiles["account-a"] = (self.profile_path, spec)
        policy_path = self.f.root / "policy.json"
        policy = json.loads(policy_path.read_text())
        entry = policy["profiles"]["account-a"]
        entry["profile_digest"] = spec.fingerprint()
        for permissions in entry["permissions"].values():
            for permission in permissions:
                permission["resource_version"] = spec.fingerprint()
        policy_path.write_text(json.dumps(policy))
        await self.ssh(
            "mkdir -m 700 "
            + shlex.quote(self.guest)
            + " && mkdir "
            + shlex.quote(self.guest + "/control")
            + " && tar -xzf "
            + shlex.quote(c["guest_archive"])
            + " -C "
            + shlex.quote(self.guest)
        )
        client = json.loads(self.f.client_file.read_text())
        for field, filename in [
            ("ca_file", "ca.pem"),
            ("certificate_file", "client.pem"),
            ("key_file", "client.key"),
        ]:
            client[field] = self.guest + "/" + filename
        (self.f.root / "guest-client.json").write_text(json.dumps(client))
        for src, name in [
            (self.profile_path, "profile.json"),
            (self.f.root / "guest-client.json", "client.json"),
            *[(self.f.root / n, n) for n in ("ca.pem", "client.pem", "client.key")],
        ]:
            await self.command(
                "scp",
                *self.options,
                str(src),
                self.target + ":" + self.guest + "/" + name,
            )
        await self.ssh("chmod 600 " + self.guest + "/client.key")
        self.original = self.f.server.handle

        async def capture(cert, expiry, envelope):
            self.messages.append(copy.deepcopy(envelope))
            if (
                envelope["message"]["method"] == "runtime.complete"
                and envelope["message"]["params"]["binding"][0] == self.hold_run
            ):
                self.complete_waiting.set()
                await self.release_complete.wait()
                raise RuntimeErrorCode("WORKER_GATEWAY_UNAVAILABLE")
            return await self.original(cert, expiry, envelope)

        self.f.server.handle = capture

    async def launch(self, label, service=False):
        name = "dios-r05-" + uuid4().hex[:10]
        self.names.append(name)
        args = [
            "sudo",
            "docker",
            "create",
            "--name",
            name,
            "--network",
            "host",
            "--restart=no",
            "--label",
            "dios.r05=" + self.f.store.scope,
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount",
            "type=bind,src=" + self.guest + ",dst=" + self.guest,
            "--mount",
            "type=bind,src=" + self.guest + "/app,dst=/app/app,readonly",
            "--entrypoint",
            "python",
            self.config["backend_image"],
            "-m",
            "app.runtime.cli",
            "--scope",
            self.f.store.scope,
            "remote-worker",
            "--profile",
            self.guest + "/profile.json",
            "--control-file",
            self.guest + "/client.json",
            "--artifacts",
            self.guest + "/spool-" + label,
            "--timeout",
            "240",
            "--poll-interval",
            ".2",
        ]
        if not service:
            args.append("--once")
        await self.ssh(shlex.join(args))
        if service:
            unit = name + ".service"
            self.units.append(unit)
            body = (
                "[Unit]\nAfter=docker.service network-online.target\nRequires=docker.service\nWants=network-online.target\n[Service]\nExecStart=/usr/bin/docker start -a "
                + name
                + "\nExecStop=/usr/bin/docker stop --time 30 "
                + name
                + "\nRestart=on-failure\nRestartSec=2\nTimeoutStopSec=45\n[Install]\nWantedBy=multi-user.target\n"
            )
            source = self.output / unit
            source.write_text(body)
            await self.command(
                "scp",
                *self.options,
                str(source),
                self.target + ":" + self.guest + "/" + unit,
            )
            await self.ssh(
                "sudo cp "
                + self.guest
                + "/"
                + unit
                + " /etc/systemd/system/"
                + unit
                + " && sudo systemctl daemon-reload && sudo systemctl enable --now "
                + unit
            )
        else:
            await self.ssh("sudo docker start " + name)
            self.assertEqual(
                (await self.ssh("sudo docker wait " + name, timeout=30)).strip(), "0"
            )
        return name

    async def submit(self, label, barrier=False):
        spec = self.f.profiles["account-a"][1]
        run = await self.f.store.submit(
            "reboot-" + label,
            {
                "runtime_profile": "account-a",
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "payload": {"barrier": barrier},
            },
            authorization=self.f.policy.admission("account-a", spec.fingerprint(), []),
        )
        self.runs[label] = run
        return run

    async def cleanup_guest(self):
        self.release_complete.set()
        try:
            snapshots = {
                name: await self.f.store.get(run["id"])
                for name, run in self.runs.items()
            }
            (self.output / "runs.json").write_text(
                json.dumps(snapshots, ensure_ascii=False, indent=2)
            )
            (self.output / "messages.json").write_text(
                json.dumps(self.messages, ensure_ascii=False, indent=2)
            )
            for unit in self.units:
                await self.ssh("sudo systemctl disable --now " + unit, timeout=55)
            for name in self.names:
                text = await self.ssh("sudo docker logs " + name + " 2>&1 || true")
                (self.output / (name + ".log")).write_text(text)
            # Remove only containers owned by this fixture's exact scope/labels.
            scope = hashlib.sha256(self.f.store.scope.encode()).hexdigest()
            for label in (
                "dios.r05=" + self.f.store.scope,
                "dios.runtime.scope=" + scope,
            ):
                ids = (
                    await self.ssh(
                        "sudo docker ps -aq --filter " + shlex.quote("label=" + label)
                    )
                ).split()
                if ids:
                    await self.ssh("sudo docker rm -f " + shlex.join(ids))
            if self.units:
                await self.ssh(
                    "sudo rm -- "
                    + shlex.join(["/etc/systemd/system/" + u for u in self.units])
                    + " && sudo systemctl daemon-reload"
                )
            (self.output / "cleanup.json").write_text(
                json.dumps(
                    {
                        "units_disabled": self.units,
                        "containers_removed_by_scope": self.f.store.scope,
                        "guest_evidence_retained": self.guest,
                    }
                )
            )
        finally:
            print("R05 retained evidence: " + str(self.output), flush=True)

    async def test_boot_change_reconciles_each_task_and_rejects_old_lease(self):
        before_boot = (await self.ssh("cat /proc/sys/kernel/random/boot_id")).strip()
        done = await self.submit("completed")
        await self.launch("completed")
        done_before = await self.wait_run(done, lambda r: r["state"] == "succeeded")
        active = await self.submit("partial", True)
        await self.launch("partial", True)
        active_before = await self.wait_run(
            active, lambda r: r["attempts"] and r["attempts"][0]["state"] == "streaming"
        )
        held = await self.submit("complete-output")
        self.hold_run = held["id"]
        await self.launch("complete-output", True)
        await asyncio.wait_for(self.complete_waiting.wait(), 30)
        held_before = await self.f.store.get(held["id"])
        queued = await self.submit("queued")
        for value in (active_before, held_before):
            self.assertEqual(
                value["attempts"][0]["remote"]["launch"]["worker_boot_id"], before_boot
            )
        self.assertEqual((await self.f.store.get(queued["id"]))["attempts"], [])
        before = {
            "completed": done_before,
            "partial": active_before,
            "complete-output": held_before,
            "queued": await self.f.store.get(queued["id"]),
        }
        (self.output / "before.json").write_text(
            json.dumps(before, ensure_ascii=False, indent=2)
        )
        await self.ssh("sync")
        started = time.monotonic()
        reboot_message_offset = len(self.messages)
        await self.command("virsh", "reset", self.config["domain"])
        self.release_complete.set()
        after_boot = before_boot
        async with asyncio.timeout(90):
            while after_boot == before_boot:
                try:
                    after_boot = (
                        await self.ssh("cat /proc/sys/kernel/random/boot_id", timeout=8)
                    ).strip()
                except (subprocess.SubprocessError, OSError):
                    await asyncio.sleep(1)
        self.assertNotEqual(after_boot, before_boot)
        final = {}
        for label, run in self.runs.items():
            final[label] = await self.wait_run(
                run, lambda r: r["state"] in {"succeeded", "failed", "cancelled"}
            )
        self.assertEqual(
            {k: v for k, v in final["completed"].items() if k != "observed_at"},
            {k: v for k, v in done_before.items() if k != "observed_at"},
        )
        self.assertEqual(final["partial"]["state"], "failed")
        self.assertEqual(
            final["partial"]["attempts"][0]["error"]["code"], "RESULT_INCOMPLETE"
        )
        self.assertEqual(final["complete-output"]["state"], "succeeded")
        self.assertEqual(final["queued"]["state"], "succeeded")
        self.assertEqual(
            final["queued"]["attempts"][0]["remote"]["launch"]["worker_boot_id"],
            after_boot,
        )
        for label, value in final.items():
            self.assertEqual(len(value["attempts"]), 1, label)
            if label != "queued":
                for field in ("id", "session_id", "process_instance"):
                    self.assertEqual(
                        value["attempts"][0][field], before[label]["attempts"][0][field]
                    )
        # Original machine certificate is retained; old process/lease bindings cannot write.
        transport = RemoteToolProvider(
            self.f.client_file,
            SimpleNamespace(scope=self.f.store.scope),
            "account-a",
            self.f.profiles["account-a"][1],
        )
        rejected = []
        for method in ("heartbeat", "report"):
            sent = copy.deepcopy(
                next(
                    e
                    for e in self.messages
                    if e["message"]["method"] == "runtime." + method
                    and e["message"]["params"]["binding"][0] == active["id"]
                )
            )
            sent["request_id"] = uuid4().hex
            with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
                await transport.exchange(sent)
            rejected.append(method)
        async with asyncio.timeout(20):
            while True:
                status = await self.f.store.runtime_status()
                if status["actual_concurrency"] == 0:
                    break
                await asyncio.sleep(0.2)
        self.assertFalse(status["attempts"])
        recovered_authorizations = [
            e["message"]["params"]
            for e in self.messages[reboot_message_offset:]
            if e["message"]["method"] == "runtime.authorize"
            and e["message"]["params"]["action"] == "accept_result"
            and e["message"]["params"]["binding"][0] == held["id"]
        ]
        self.assertTrue(recovered_authorizations)
        held_attempt = held_before["attempts"][0]
        self.assertTrue(
            all(
                e["binding"][1] == held_attempt["id"]
                and e["session_id"] == held_attempt["session_id"]
                and e["binding"][4] == held_attempt["epoch"]
                for e in recovered_authorizations
            )
        )
        for label, run in self.runs.items():
            self.assertEqual(
                (await self.f.store.get(run["id"]))["state"], final[label]["state"]
            )
        observation = {
            "case": "R05",
            "domain": self.config["domain"],
            "domain_uuid": self.config["domain_uuid"],
            "boot_before": before_boot,
            "boot_after": after_boot,
            "fault": "virsh reset",
            "automatic_restart": "systemd enabled remote Worker services",
            "seconds_to_all_reconciled": time.monotonic() - started,
            "final_runs": final,
            "before_runs": before,
            "old_lease_rejected": rejected,
            "recovery_authorization_requests": recovered_authorizations,
            "work_authorization_ref": held_before["input"]["work_authorization_ref"],
            "actual_concurrency_after": status["actual_concurrency"],
            "worker_image": self.config["backend_image"],
            "runtime_code": "current backend/app mounted read-only",
            "model": "synthetic JSONL endpoint; no model calls",
            "physical_hosts": 1,
        }
        OBSERVATIONS.append(observation)
        (self.output / "result.json").write_text(
            json.dumps(observation, ensure_ascii=False, indent=2)
        )
