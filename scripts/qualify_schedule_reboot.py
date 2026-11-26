"""Reboot only a prepared disposable VM; retain task and artifact identity evidence."""

import argparse
import concurrent.futures
import json
import shlex
import subprocess
import time
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Qualify schedules in an explicitly prepared disposable Linux VM"
)
parser.add_argument("--workspace", type=Path, required=True)
parser.add_argument("--resume-drained", action="store_true")
args = parser.parse_args()
root = args.workspace.resolve()
config = json.loads((root / "launch.json").read_text())
options = [
    "-i",
    str(root / "guest_key"),
    "-o",
    "BatchMode=yes",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "ConnectTimeout=3",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "UserKnownHostsFile=" + str(root / "known_hosts"),
]
ssh = ["ssh", *options, "-p", str(config["ssh_port"]), "ubuntu@127.0.0.1"]


def remote(arguments, **kwargs):
    result = subprocess.run(
        [*ssh, shlex.join(arguments)],
        text=True,
        capture_output=True,
        check=False,
        **kwargs,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-6000:])
    return result.stdout


def driver(command):
    return json.loads(
        remote(
            [
                "sudo",
                "docker",
                "exec",
                "dios-qualification-control",
                "python",
                "/opt/dios-qualification/driver.py",
                command,
            ],
            timeout=30,
        )
    )


resuming = args.resume_drained
checks = json.loads((root / "checks.json").read_text()) if resuming else []


def save(name, value):
    (root / (name + ".json")).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    )


def record(name):
    checks.append(name)
    save("checks", checks)
    print(name, flush=True)


def status():
    return driver("status")


def wait(predicate, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = status()
        if predicate(value):
            return value
        time.sleep(0.25)
    save("timeout", value)
    raise AssertionError("state observation timed out")


def tick_three():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: driver("tick"), range(3)))


def worker():
    return subprocess.Popen(
        [
            *ssh,
            shlex.join(
                [
                    "sudo",
                    "docker",
                    "exec",
                    "dios-qualification-control",
                    "python",
                    "/opt/dios-qualification/driver.py",
                    "worker",
                ]
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def collect(process):
    out, err = process.communicate(timeout=45)
    assert process.returncode == 0, err
    return json.loads(out)


assert remote(["cat", "/etc/hostname"]).strip() == "dios-qualification"
assert (
    remote(
        [
            "sudo",
            "docker",
            "exec",
            "dios-qualification-control",
            "python",
            "-c",
            "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status)",
        ]
    ).strip()
    == "200"
)
if not resuming:
    initial = status()
    assert not initial["runs"]
    save("initial", initial)
    driver("create")
    driver("due")
    tick_three()
    first = wait(lambda s: len(s["runs"]) == 1)
    assert len(first["work"]) == 1
    record("three-independent-scheduler-processes-create-one-run-and-work")
    process = worker()
    active = wait(lambda s: s["runtime"]["actual_concurrency"] == 1)
    assert len(active["runs"][0]["attempts"]) == 1
    save("active", active)
else:
    active = json.loads((root / "active.json").read_text())
run_id = active["runs"][0]["id"]
attempt_id = active["runs"][0]["attempt_id"]
session_id = active["runs"][0]["attempts"][0]["session_id"]
if not resuming:
    driver("overlap")
    tick_three()
    overlap = status()
    assert len(overlap["runs"]) == 1 and len(overlap["work"]) == 1
    assert len(overlap["occurrences"]) >= 2
    save("overlap", overlap)
    record("overlapping-ticks-buffer-without-replacing-active-run")
    draining = driver("drain")
    assert draining["active_attempts"] == 1 and not draining["ready_for_cutover"]
    idle = driver("worker")
    assert idle["state"] == "idle"
    tick_three()
    held = status()
    assert len(held["runs"]) == 1 and held["runtime"]["actual_concurrency"] == 1
    record("drain-keeps-original-process-and-blocks-new-work")
    driver("release")
    completed = collect(process)
    assert completed["state"] == "succeeded"
tick_three()
before = wait(lambda s: s["lifecycle"]["ready_for_cutover"])
assert (
    before["runs"][0]["id"] == run_id and before["runs"][0]["attempt_id"] == attempt_id
)
assert before["runs"][0]["attempts"][0]["session_id"] == session_id
assert before["runs"][0]["attempts"][0]["remote"]["runtime"]["alive"] is False
assert len(before["artifacts"]) >= 2
result_artifacts = [
    a for a in before["details"][0]["artifacts"] if a["kind"] == "result"
]
assert len(result_artifacts) == 1
assert result_artifacts[0]["digest"] in {
    a["verified_sha256"] for a in before["artifacts"]
}
save("before-reboot", before)
record("natural-exit-retains-original-attempt-session-and-verified-artifacts")
before_container = json.loads(
    remote(["sudo", "docker", "inspect", "dios-qualification-control"])
)[0]
save(
    "container-before",
    {
        "id": before_container["Id"],
        "image": before_container["Image"],
        "started_at": before_container["State"]["StartedAt"],
    },
)
subprocess.run(
    [*ssh, shlex.join(["sudo", "systemctl", "reboot"])],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    timeout=15,
)
deadline = time.monotonic() + 120
last_error = ""
while time.monotonic() < deadline:
    try:
        boot = remote(["cat", "/proc/sys/kernel/random/boot_id"], timeout=5).strip()
        if boot != before["boot_id"]:
            after = status()
            break
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        last_error = type(exc).__name__
    time.sleep(1)
else:
    raise AssertionError("guest reboot observation timed out: " + last_error)
save("after-reboot", after)
assert after["boot_id"] != before["boot_id"]
assert (
    after["lifecycle"]["mode"] == "draining" and after["lifecycle"]["ready_for_cutover"]
)
assert [(r["id"], r["attempt_id"], r["state"]) for r in after["runs"]] == [
    (r["id"], r["attempt_id"], r["state"]) for r in before["runs"]
]
assert {a["id"]: a["verified_sha256"] for a in after["artifacts"]} == {
    a["id"]: a["verified_sha256"] for a in before["artifacts"]
}
assert {w["id"] for w in after["work"]} == {w["id"] for w in before["work"]}
old_schedule = before["schedules"]["items"][0]
new_schedule = after["schedules"]["items"][0]
assert (
    new_schedule["id"] == old_schedule["id"]
    and new_schedule["next_due"] >= old_schedule["next_due"]
)
assert {o["id"] for o in before["occurrences"]} <= {
    o["id"] for o in after["occurrences"]
}
after_container = json.loads(
    remote(["sudo", "docker", "inspect", "dios-qualification-control"])
)[0]
assert (
    after_container["Id"] == before_container["Id"]
    and after_container["Image"] == before_container["Image"]
)
assert after_container["State"]["StartedAt"] != before_container["State"]["StartedAt"]
save(
    "container-after",
    {
        "id": after_container["Id"],
        "image": after_container["Image"],
        "started_at": after_container["State"]["StartedAt"],
    },
)
record("kernel-reboot-preserves-drain-cursor-run-work-and-artifact-identities")
assert (
    remote(
        [
            "sudo",
            "docker",
            "exec",
            "dios-qualification-control",
            "python",
            "-c",
            "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status)",
        ]
    ).strip()
    == "200"
)
driver("resume")
tick_three()
resumed = wait(lambda s: len(s["runs"]) >= 2)
assert resumed["runs"][0]["id"] == run_id
second = resumed["runs"][1]
assert (
    second["id"] != run_id
    and second["input"]["work_authorization_ref"]
    != before["runs"][0]["input"]["work_authorization_ref"]
)
success = driver("worker")
assert success["id"] == second["id"] and success["state"] == "succeeded"
driver("drain")
tick_three()
final = status()
assert final["lifecycle"]["ready_for_cutover"]
assert len([a for r in final["runs"] for a in r["attempts"]]) == 2
save("final", final)
record("resume-delivers-buffered-occurrence-once-with-new-work")
save(
    "result",
    {
        "passed": True,
        "checks": checks,
        "boot_before": before["boot_id"],
        "boot_after": after["boot_id"],
        "run_ids": [r["id"] for r in final["runs"]],
        "attempt_ids": [a["id"] for r in final["runs"] for a in r["attempts"]],
        "artifact_count": len(final["artifacts"]),
        "kernel": remote(["uname", "-r"]).strip(),
        "backend_image": after_container["Image"],
    },
)
print("PASS", flush=True)
