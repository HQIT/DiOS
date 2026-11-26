"""Qualify real Prometheus scraping, alert transitions and persisted history."""

import argparse, json, os, signal, subprocess, time
from pathlib import Path
import docker, httpx

p = argparse.ArgumentParser()
p.add_argument("--root", type=Path, required=True)
args = p.parse_args()
root = args.root.resolve()
base = "http://127.0.0.1:13350"
report = json.loads((root/"stack.json").read_text()) if (root/"stack.json").exists() else {"passed":False,"checks":[]}
client = httpx.Client(trust_env=False, timeout=5)
dc = docker.from_env()
container = dc.containers.get("dios-monitor-qualification-prometheus-1")
assert (
    container.labels.get("com.docker.compose.project") == "dios-monitor-qualification"
)


def get(path, **params):
    r = client.get(base + path, params=params)
    r.raise_for_status()
    return r.json()["data"]


def wait(name, predicate, timeout=140):
    if name in report["checks"]:return
    start = time.monotonic()
    last = -20
    while time.monotonic() - start < timeout:
        container.reload()
        assert container.status == "running", container.status
        try:
            if predicate():
                report["checks"].append(name)
                print(name + ": passed", flush=True)
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() - start - last >= 20:
            print(name + ": waiting for live observation", flush=True)
            last = time.monotonic() - start
        time.sleep(2)
    raise AssertionError(name + " did not meet its condition")


def firing(name):
    return any(
        x["labels"]["alertname"] == name and x["state"] == "firing"
        for x in get("/api/v1/alerts")["alerts"]
    )


try:
    wait(
        "actual-scrape",
        lambda: any(
            x["health"] == "up" for x in get("/api/v1/targets")["activeTargets"]
        ),
    )
    wait(
        "missing-observation-fires",
        lambda: firing("DiOSSchedulerObservationUnavailable"),
    )
    (root / "fixture/state").write_text("healthy")
    wait("recovery-clears-alert", lambda: not get("/api/v1/alerts")["alerts"])
    sample_at = time.time() - 15
    before = get("/api/v1/query", query='up{job="dios-schedulers"}', time=sample_at)[
        "result"
    ]
    assert before and before[0]["value"][1] == "1"
    flags = get("/api/v1/status/flags")
    assert (
        flags["storage.tsdb.retention.time"] == "15d"
        and flags["storage.tsdb.retention.size"] == "1GiB"
    )
    assert (
        flags["web.enable-admin-api"] == "false"
        and flags["web.enable-lifecycle"] == "false"
    )
    assert client.post(base + "/-/reload").status_code == 403
    denied=client.post(base+"/api/v1/admin/tsdb/delete_series",params={"match[]":"up"})
    assert denied.status_code==500 and denied.json().get("error")=="admin APIs disabled"
    report["checks"].append("retention-and-admin-interfaces")
    config = get("/api/v1/status/config")["yaml"]
    assert "remote_write" not in config and "alertmanagers:" not in config
    container.restart(timeout=10)
    wait(
        "restart-replays-history",
        lambda: (
            get("/api/v1/query", query='up{job="dios-schedulers"}', time=sample_at)[
                "result"
            ]
            == before
        ),
    )
    pid = json.loads((root / "fixture/ready.json").read_text())["pid"]
    assert b"serve_monitor_fixture.py" in Path(f"/proc/{pid}/cmdline").read_bytes()
    os.kill(pid, signal.SIGTERM)
    wait("endpoint-loss-fires", lambda: firing("DiOSSchedulerMetricsUnavailable"))
    report.update(
        passed=True,
        retention={"time": "15d", "size": "1GiB", "history_after_restart": True},
        notifications="local Prometheus alerts; no external receiver",
        prometheus_image=container.image.id,
    )
finally:
    (root / "stack.json").write_text(json.dumps(report, indent=2) + "\n")
    client.close()
    dc.close()
