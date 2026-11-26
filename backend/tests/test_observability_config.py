"""Deployment defaults and validation, independent from runtime regressions."""

import importlib.util, json, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "monitor_init", ROOT / "deploy/observability/init.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = SimpleNamespace(
            root=Path(self.temp.name) / "monitor",
            target_url="http://127.0.0.1:13081",
            image="selected-image",
            name="dios-monitor-test",
            port=13090,
            retention_time="15d",
            retention_size="1GB",
        )

    def init(self):
        with (
            patch.dict(os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""}),
            patch.object(
                module.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="sha256:" + "a" * 64, returncode=0),
            ),
        ):
            return module.initialize(self.args)

    def test_private_pin_loopback_retention_and_no_external_notifications(self):
        result = self.init()
        root = self.args.root
        compose = json.loads((root / "compose.yaml").read_text())
        service = compose["services"]["prometheus"]
        config = json.loads((root / "config/prometheus.yml").read_text())
        self.assertEqual(
            json.loads((root / "backend-monitoring.compose.yaml").read_text())[
                "services"
            ]["backend"]["environment"]["DIOS_RUNTIME_MONITOR_TOKEN_FILE"],
            "/run/dios-monitor/monitor.token",
        )
        self.assertEqual(result["notification_receivers"], [])
        self.assertTrue(service["image"].startswith("sha256:"))
        self.assertIn("--web.listen-address=127.0.0.1:13090", service["command"])
        self.assertIn("--storage.tsdb.retention.time=15d", service["command"])
        self.assertIn("--storage.tsdb.retention.size=1GB", service["command"])
        self.assertFalse(
            any(
                "enable-admin-api" in s or "enable-lifecycle" in s
                for s in service["command"]
            )
        )
        self.assertEqual((root / "config/monitor.token").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            config["scrape_configs"][0]["authorization"],
            {"credentials_file": "/etc/prometheus/monitor.token"},
        )
        self.assertNotIn(
            (root / "config/monitor.token").read_text().strip(),
            json.dumps(compose) + json.dumps(config),
        )
        self.assertFalse(config["scrape_configs"][0]["follow_redirects"])
        self.assertNotIn("alerting", config)
        self.assertNotIn("remote_write", config)
        old = (root / "config/monitor.token").read_bytes()
        with self.assertRaises(ValueError):
            self.init()
        self.assertEqual((root / "config/monitor.token").read_bytes(), old)

    def test_remote_http_credentials_and_bad_retention_rejected(self):
        for url in [
            "http://remote.example",
            "https://user:password@example.com",
            "https://example.com?token=bad",
        ]:
            self.args.target_url = url
            with self.assertRaises(ValueError):
                self.init()
        self.args.target_url = "https://metrics.example"
        self.args.retention_time = "0d"
        with self.assertRaises(ValueError):
            self.init()
        self.assertFalse(self.args.root.exists())

    def test_https_target_is_preserved(self):
        self.args.target_url = "https://metrics.example/dios"
        self.init()
        cfg = json.loads((self.args.root / "config/prometheus.yml").read_text())[
            "scrape_configs"
        ][0]
        self.assertEqual(cfg["scheme"], "https")
        self.assertEqual(cfg["metrics_path"], "/dios/api/os/runtime/scheduler-metrics")
