"""Real DSH HTTP errors through mTLS Workers and E2AG recovery."""

import hashlib
import json
import os
from pathlib import Path
import re
import unittest

import test_remote_recovery_qualification as recovery

OBSERVATIONS = []


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DSH_PROVIDER_IMAGE"), "qualified DSH image required"
)
class ProviderFailureQualificationTests(recovery.RemoteRecoveryQualificationTests):
    workload_image = os.environ.get("DIOS_TEST_DSH_PROVIDER_IMAGE", "unconfigured")

    def configure_launch(self, profile, value):
        value["launch"].update(
            command=["python", "-u", "/probe.py", profile],
            memory_bytes=1024 * 1024 * 1024,
            cpu_count=1,
            pids_limit=128,
            environment={
                "HOME": "/tmp",
                "DSH_HOME": "/tmp/dsh",
                "NARB_DISABLE_NATIVE_CACHE": "1",
                "DSH_PERMISSION_MODE": "read-only",
                "DSH_TELEMETRY_DISABLED": "1",
                "DSH_ECNU_API_KEY": "synthetic-test-only",
                **{
                    key: ""
                    for key in (
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "http_proxy",
                        "https_proxy",
                        "ALL_PROXY",
                        "all_proxy",
                    )
                },
                "NO_PROXY": "*",
                "no_proxy": "*",
            },
        )
        value["launch"]["mounts"].append(
            {
                "source": str(
                    recovery.ROOT / "scripts/fixtures/dsh_provider_failure_probe.py"
                ),
                "target": "/probe.py",
                "read_only": True,
            }
        )

    async def qualification(self, code):
        await super().qualification(code)
        row = dict(
            recovery.OBSERVATIONS[-1], scenario="R06", transport="actual-http-dsh-acp"
        )
        retained = []
        frames = []
        for path in self.root.glob("worker-*/*"):
            if not path.is_file() or not re.fullmatch("[0-9a-f]{64}", path.name):
                continue
            raw = path.read_bytes()
            if b"provider_http" not in raw:
                continue
            self.assertEqual(hashlib.sha256(raw).hexdigest(), path.name)
            events = [json.loads(line) for line in raw.splitlines()]
            frames.extend(events)
            retained.append({"sha256": path.name, "events": events})
        http = [
            json.loads(event["text"])
            for event in frames
            if event.get("type") == "output"
            and "provider_http" in event.get("text", "")
        ]
        self.assertEqual(
            sorted(event["provider_http"] for event in http), [200] * 5 + [429]
        )
        errors = [event["code"] for event in frames if event.get("type") == "error"]
        self.assertEqual(errors, [code])
        sessions = [
            event["session_id"] for event in frames if event.get("type") == "session"
        ]
        self.assertEqual(len(set(sessions)), 6)
        row.update(http_responses=http, artifacts=retained, distinct_harness_sessions=6)
        OBSERVATIONS.append(row)
        if os.environ.get("DIOS_PROVIDER_EVIDENCE"):
            target = Path(os.environ["DIOS_PROVIDER_EVIDENCE"])
            target.mkdir(parents=True, exist_ok=True)
            (target / (code + ".json")).write_text(
                json.dumps(row, ensure_ascii=False, indent=2) + "\n"
            )
