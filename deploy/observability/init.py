"""Initialize a Linux-local Prometheus deployment with independent scrape credentials."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent


def initialize(args):
    root = args.root.absolute()
    if (
        root.exists()
        or root.resolve() != root
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root))
    ):
        raise ValueError("NEW_LOCAL_DIRECTORY_REQUIRED")
    target = urlsplit(args.target_url)
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username
        or target.password
        or target.query
        or target.fragment
    ):
        raise ValueError("INVALID_TARGET_URL")
    if target.scheme == "http" and target.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("REMOTE_TARGET_REQUIRES_HTTPS")
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.name)
        or not 1024 <= args.port <= 65535
    ):
        raise ValueError("INVALID_SERVICE_CONFIGURATION")
    if not re.fullmatch(
        r"[1-9][0-9]{0,3}[dh]", args.retention_time
    ) or not re.fullmatch(r"[1-9][0-9]{0,3}(MB|GB)", args.retention_size):
        raise ValueError("INVALID_RETENTION")
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise ValueError("LOCAL_DOCKER_REQUIRED")
    result = subprocess.run(
        [
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "image",
            "inspect",
            args.image,
            "--format",
            "{{.Id}}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    image = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("LOCAL_PROMETHEUS_IMAGE_REQUIRED")
    root.mkdir(parents=True, mode=0o700)
    for name in ["config", "data"]:
        (root / name).mkdir(mode=0o700)

    def write(path, value):
        with path.open("x") as f:
            os.chmod(path, 0o600)
            f.write(value)

    write(root / "config/monitor.token", secrets.token_urlsafe(32) + "\n")
    scrape = {
        "job_name": "dios-schedulers",
        "scrape_interval": "15s",
        "scrape_timeout": "10s",
        "scheme": target.scheme,
        "metrics_path": target.path.rstrip("/") + "/api/os/runtime/scheduler-metrics",
        "authorization": {"credentials_file": "/etc/prometheus/monitor.token"},
        "static_configs": [{"targets": [target.netloc]}],
        "sample_limit": 100,
        "label_limit": 8,
        "body_size_limit": "64KB",
        "follow_redirects": False,
    }
    config = {
        "global": {"scrape_interval": "15s", "evaluation_interval": "15s"},
        "rule_files": ["/etc/prometheus/scheduler-alerts.yml"],
        "scrape_configs": [scrape],
    }
    write(root / "config/prometheus.yml", json.dumps(config, indent=2) + "\n")
    shutil.copyfile(HERE / "scheduler-alerts.yml", root / "config/scheduler-alerts.yml")
    commands = [
        "--config.file=/etc/prometheus/prometheus.yml",
        "--storage.tsdb.path=/prometheus",
        "--storage.tsdb.retention.time=" + args.retention_time,
        "--storage.tsdb.retention.size=" + args.retention_size,
        "--web.listen-address=127.0.0.1:" + str(args.port),
    ]
    compose = {
        "name": args.name,
        "services": {
            "prometheus": {
                "image": image,
                "pull_policy": "never",
                "user": str(os.getuid()) + ":" + str(os.getgid()),
                "network_mode": "host",
                "restart": "unless-stopped",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "mem_limit": "512m",
                "pids_limit": 128,
                "volumes": [
                    str(root / "config") + ":/etc/prometheus:ro",
                    str(root / "data") + ":/prometheus",
                ],
                "command": commands,
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3"},
                },
            }
        },
    }
    write(root / "compose.yaml", json.dumps(compose, indent=2) + "\n")
    overlay = {
        "services": {
            "backend": {
                "environment": {
                    "DIOS_RUNTIME_MONITOR_TOKEN_FILE": "/run/dios-monitor/monitor.token"
                },
                "volumes": [str(root / "config") + ":/run/dios-monitor:ro"],
            }
        }
    }
    write(
        root / "backend-monitoring.compose.yaml", json.dumps(overlay, indent=2) + "\n"
    )
    write(
        root / "backend.env",
        "DIOS_RUNTIME_MONITOR_TOKEN_FILE=" + str(root / "config/monitor.token") + "\n",
    )
    return {
        "root": str(root),
        "image": image,
        "target": args.target_url,
        "local_ui": "http://127.0.0.1:" + str(args.port),
        "retention": {"time": args.retention_time, "size": args.retention_size},
        "notification_receivers": [],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--target-url", required=True)
    p.add_argument("--name", default="dios-observability")
    p.add_argument("--port", type=int, default=13090)
    p.add_argument("--retention-time", default="15d")
    p.add_argument("--retention-size", default="1GB")
    try:
        print(json.dumps(initialize(p.parse_args())))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        p.exit(
            1,
            "Monitoring configuration failed: "
            + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
