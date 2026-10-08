"""Local AMQP qualification; broker lifecycle and credentials are isolated.

Uses a cached, pinned broker image, real AMQP deliveries and the same native
governance worker as file qualification. Not a production Connector deployment.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

from amqp_ingress import connect, pika
from run_file_recovery import ROOT, CUTS, save_new, append_record, tool_endpoint
import httpx

IMAGE = "sha256:1031d41f3f1611f61f0fe9011e26201967f460f93f96f92d5be0e823d59e7541"
WORKER = Path(__file__).with_name("run_file_recovery.py")
CASES = [(mode, mode, None) for mode in
         ["normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"]]
CASES += [(cut, "normal", cut) for cut in
          ["before_receive_commit", "after_receive_commit", "after_handoff_commit"]]
CASES += [("pending_redelivery", "pending", "after_handoff_commit"),
          ("approved_redelivery", "approved", "after_handoff_commit")]
CASES += [(name, "normal", None) for name in
          ["identity_conflict", "distinct_identity", "nested_source", "bad_json", "reserved_source", "message_id_mismatch"]]


def docker(*args):
    result = subprocess.run(["wsl", "-d", "Ubuntu-24.04", "--", "docker", *args],
                            capture_output=True, timeout=90)
    if result.returncode:
        # Do not include command arguments (may contain ephemeral credentials).
        raise RuntimeError("Docker operation failed: " + result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8").strip()


@contextmanager
def broker(output):
    # A foreground WSL process keeps the distribution alive while Windows
    # workers use its loopback ports; background Docker alone may not do so.
    keepalive = subprocess.Popen(["wsl", "-d", "Ubuntu-24.04", "--", "cat"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    admin_password = secrets.token_urlsafe(24)
    consumer_password, producer_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    name = "e2ag-rr022-amqp-20261007-" + uuid.uuid4().hex[:8]
    container = docker("run", "-d", "--name", name, "--memory", "512m", "--cpus", "1",
        "--tmpfs", "/var/lib/rabbitmq", "-p", "127.0.0.1::5672", "-p", "127.0.0.1::15672",
        "-e", "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 2:2",
        "-e", "RABBITMQ_DEFAULT_USER=e2ag_admin", "-e", "RABBITMQ_DEFAULT_PASS=" + admin_password,
        "-e", "RABBITMQ_DEFAULT_VHOST=e2ag", IMAGE)
    try:
        ports = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", container))
        port = int(ports["5672/tcp"][0]["HostPort"])
        management_port = int(ports["15672/tcp"][0]["HostPort"])
        with httpx.Client(base_url=f"http://127.0.0.1:{management_port}/api",
                          auth=("e2ag_admin", admin_password), trust_env=False, timeout=3) as client:
            deadline = time.monotonic() + 60
            overview = None
            while time.monotonic() < deadline:
                try:
                    response = client.get("/overview")
                    if response.status_code == 200:
                        overview = response.json()
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.5)
            if overview is None:
                raise RuntimeError("Isolated broker did not become ready")
            for user, password, read, write in [
                ("e2ag_consumer", consumer_password, "^e2agq_.*$", "(?!)"),
                ("e2ag_producer", producer_password, "(?!)", "^e2ag.events$")]:
                client.put("/users/" + user, json={"password": password, "tags": []}).raise_for_status()
                client.put("/permissions/e2ag/" + user,
                    json={"configure": "(?!)", "write": write, "read": read}).raise_for_status()
            admin = connect(port, username="e2ag_admin", password=admin_password)
            channel = admin.channel()
            channel.exchange_declare("e2ag.events", exchange_type="direct", durable=True)
            channel.exchange_declare("foreign.events", exchange_type="direct", durable=True)
            channel.queue_declare("foreign_queue", durable=True)
            info = {"container": name, "container_id": container, "image_id": IMAGE,
                    "rabbitmq_version": overview["rabbitmq_version"], "pika_version": pika.__version__,
                    "ports": ports, "transport": "AMQP 0-9-1; loopback; no TLS",
                    "consumer_permissions": {"configure": "(?!)", "write": "(?!)", "read": "^e2agq_.*$"},
                    "producer_permissions": {"configure": "(?!)", "write": "^e2ag.events$", "read": "(?!)"}}
            save_new(output / "broker.json", info)
            print(json.dumps({"broker": "ready", "rabbitmq": info["rabbitmq_version"]}), flush=True)
            environment = {**os.environ, "E2AG_AMQP_USER": "e2ag_consumer", "E2AG_AMQP_PASSWORD": consumer_password}
            try:
                yield port, channel, environment, producer_password
            finally:
                if admin.is_open:
                    admin.close()
    finally:
        try:
            status = json.loads(docker("inspect", "--format", "{{json .State}}", container))
            logs = docker("logs", "--tail", "300", container)
            for secret in [admin_password, consumer_password, producer_password]:
                logs = logs.replace(secret, "[REDACTED]")
            save_new(output / "broker-final-state.json", {"state": status, "logs": logs})
            docker("stop", "--time", "5", container)
            docker("rm", container)
            save_new(output / "broker-cleanup.json", {"container_id": container,
                     "stopped_and_removed": True, "ephemeral_broker_data_removed": True})
        finally:
            keepalive.stdin.close()
            keepalive.wait(timeout=15)


def publish(port, password, queue, body, message_id):
    connection = connect(port, username="e2ag_producer", password=password)
    try:
        channel = connection.channel()
        channel.confirm_delivery()
        channel.basic_publish("e2ag.events", queue, body, mandatory=True,
            properties=pika.BasicProperties(message_id=message_id, content_type="application/json", delivery_mode=2))
    finally:
        connection.close()


def acl_checks(port, channel, environment, producer_password):
    records = []
    try:
        connection = connect(port, username="e2ag_consumer", password="deliberately-wrong-password")
    except pika.exceptions.ProbableAuthenticationError as exc:
        records.append({"case": "wrong_password", "denied": True, "exception": type(exc).__name__})
    else:
        connection.close()
        records.append({"case": "wrong_password", "denied": False})
    for name, user, password, operation in [
        ("foreign_queue", "e2ag_consumer", environment["E2AG_AMQP_PASSWORD"], "read"),
        ("foreign_exchange", "e2ag_producer", producer_password, "write")]:
        connection = connect(port, username=user, password=password)
        try:
            client = connection.channel()
            if operation == "read":
                client.basic_get("foreign_queue", auto_ack=False)
            else:
                client.confirm_delivery()
                client.basic_publish("foreign.events", "unused", b"fixed ACL probe")
        except pika.exceptions.ChannelClosedByBroker as exc:
            records.append({"case": name, "denied": exc.reply_code == 403,
                            "reply_code": exc.reply_code, "reply_text": exc.reply_text})
        else:
            records.append({"case": name, "denied": False})
        finally:
            if connection.is_open:
                connection.close()
    return {"passed": all(r["denied"] for r in records), "records": records}


def run_case(folder, mode, cut, port, admin_channel, environment, producer_password, *, tool_transport="streamable_http", database_url=None):
    folder.mkdir()
    queue = "e2agq_" + folder.name
    admin_channel.queue_declare(queue, durable=True)
    admin_channel.queue_bind(queue, "e2ag.events", routing_key=queue)
    body = {"id": "delivery-1", "subject": "Qualification task",
            "data": {"text": "governance receipt", "environment": "production",
                     "requested_action": "secret.read" if mode in {"pending", "approved"} else "report.create"}}
    if folder.name == "nested_source":
        body["data"]["source"] = "trusted/forged"
    if folder.name == "reserved_source":
        body["source"] = "trusted/forged"
    raw = b"{not-json" if folder.name == "bad_json" else json.dumps(body).encode()
    message_id = "other-id" if folder.name == "message_id_mismatch" else body["id"]
    rejected = folder.name in {"bad_json", "reserved_source", "message_id_mismatch"}
    records, publications, queue_states = [], [], []
    environment = dict(environment)
    if database_url:
        environment["E2AG_QUAL_DATABASE_URL"] = database_url
    else:
        environment.pop("E2AG_QUAL_DATABASE_URL", None)

    def send(raw_body, identity):
        publish(port, producer_password, queue, raw_body, identity)
        record = {"body_utf8": raw_body.decode(), "message_id": identity, "publisher_confirmed": True}
        publications.append(record)
        append_record(folder / "publications.jsonl", record)

    def state(stage):
        # Fresh passive declaration, not cached management statistics.
        result = admin_channel.queue_declare(queue, passive=True).method
        row = {"stage": stage, "ready_messages": result.message_count, "consumers": result.consumer_count}
        queue_states.append(row)
        append_record(folder / "queue-states.jsonl", row)
        return result.message_count

    def invoke(op, url, *, interrupted=None, recover=False, expect_error=False):
        command = [sys.executable, str(WORKER), "--worker", "--directory", str(folder), "--op", op,
                   "--mode", mode, "--url", url, "--ingress", "amqp", "--broker-port", str(port), "--queue", queue,
                   "--tool-transport", tool_transport]
        if interrupted: command += ["--cut", interrupted]
        if recover: command.append("--recover")
        process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                 encoding="utf-8", timeout=40, env=environment)
        record = {"op": op, "cut": interrupted, "recover": recover, "exit_code": process.returncode,
                  "stdout": process.stdout, "stderr": process.stderr}
        records.append(record)
        append_record(folder / "processes.jsonl", record)
        if interrupted:
            assert process.returncode == CUTS[interrupted], record
            return None
        if expect_error:
            assert process.returncode != 0, record
            return {"rejected": True, "error": process.stderr.splitlines()[-1]}
        assert process.returncode == 0, record
        return json.loads(process.stdout)

    with tool_endpoint(folder) as (url, operations):
        invoke("setup", url)
        send(raw, message_id)
        first = invoke("consume", url, interrupted=cut, expect_error=rejected)
        if cut:
            deadline = time.monotonic() + 5
            while state("after_disconnect") != 1 and time.monotonic() < deadline:
                time.sleep(.1)
            # The previous subprocess is terminal before ownership takeover.
            first = invoke("consume", url, recover=True)
        state("after_first")
        if mode == "approved":
            assert invoke("approve", url) == {"approval": "approved"}
            assert invoke("approve", url) == {"approval_error": "APPROVAL_ALREADY_DECIDED"}
        if not rejected:
            send(raw, message_id)
        second = invoke("consume", url, expect_error=rejected)
        state("after_repeat")
        extra = None
        if folder.name in {"identity_conflict", "distinct_identity"}:
            if folder.name == "identity_conflict": body["data"]["text"] = "changed content"
            else: body["id"] = "delivery-2"
            send(json.dumps(body).encode(), body["id"])
            extra = invoke("consume", url)
            state("after_extra")
        snapshot = invoke("snapshot", url)
    if tool_transport == "stdio":
        log = folder / "tool-receipts.jsonl"
        operations = [json.loads(s) for s in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    count = 0 if rejected else 2 if folder.name == "distinct_identity" else 1
    expected_tools = count if mode in {"normal", "approved"} else 0
    checks = {
        "event_count": len(snapshot["events"]) == count,
        "task_count": len(snapshot["tasks"]) == (0 if mode in {"deny", "pending"} else count),
        "tool_count": len(operations) == expected_tools,
        "audit_valid": snapshot["audit_valid"],
        "source_binding": all(e["cloud_event"]["source"] == "amqp/fixture" for e in snapshot["events"]),
        "artifact_content": all(o == {"method": "tools/call", "name": "report.write", "arguments": {
            "resource": "report/allowed", "report": "GOVERNANCE RECEIPT"}} for o in operations),
        "queue_disposition": queue_states[-1]["ready_messages"] == (1 if rejected else 0),
    }
    if rejected:
        expected_error = {"bad_json": "INVALID_JSON", "reserved_source": "INVALID_ENVELOPE_FIELDS",
                          "message_id_mismatch": "MESSAGE_ID_ENVELOPE_MISMATCH"}[folder.name]
        checks["explicit_input_rejection"] = expected_error in first["error"] and expected_error in second["error"]
        checks["no_durable_handoff"] = not snapshot["receipts"]
    else:
        checks["acknowledged_after_handoff"] = first["broker"]["acknowledged"] and second["broker"]["acknowledged"]
        checks["duplicate_suppressed"] = second["disposition"] == "duplicate"
        if cut: checks["actual_broker_redelivery"] = first["broker"]["redelivered"]
        if mode in {"tool", "resource", "revoked", "expired", "revoked_reconnect"}:
            checks["explicit_pep_denial"] = snapshot["tasks"][0]["artifacts"][0]["qualification_call_status"] == 403
        if mode == "pending": checks["approval_pending"] = len(snapshot["approvals"]) == 1 and snapshot["approvals"][0]["status"] == "pending"
        if extra:
            checks["identity_semantics"] = (extra["disposition"] == "conflict" and len(snapshot["conflicts"]) == 1
                if folder.name == "identity_conflict" else extra["disposition"] == "recorded")
    return {"case": folder.name, "mode": mode, "cut": cut, "tool_transport": tool_transport, "first": first, "repeat": second,
            "extra": extra, "checks": checks, "passed": all(checks.values()), "snapshot": snapshot,
            "tool_operations": operations, "processes": records, "publications": publications,
            "queue_states": queue_states, "business_complete": expected_tools > 0 and len(operations) == expected_tools,
            "invalid_delivery_retained": rejected}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = sorted((ROOT / "backend/app").rglob("*.py")) + sorted(Path(__file__).parent.glob("*.py"))
    save_new(output / "freeze.json", {"kind": "qualification", "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "real AMQP broker; native governance; deterministic task executor; ASGI PEP ingress; real HTTP tool; SQLite",
        "cases": [{"name": n, "mode": m, "cut": c} for n, m, c in CASES],
        "source_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    rows = []
    with broker(output) as (port, channel, environment, password):
        controls = acl_checks(port, channel, environment, password)
        save_new(output / "acl-controls.json", controls)
        print(json.dumps({"acl_controls": controls["passed"]}), flush=True)
        for name, mode, cut in CASES:
            try:
                row = run_case(output / name, mode, cut, port, channel, environment, password)
            except Exception:
                import traceback
                row = {"case": name, "passed": False, "error": traceback.format_exc()}
            save_new(output / (name + ".json"), row)
            rows.append(row)
            print(json.dumps({"case": name, "passed": row["passed"], "checks": row.get("checks"), "error": row.get("error")}), flush=True)
    passed = controls["passed"] and all(r["passed"] for r in rows)
    save_new(output / "summary.json", {"status": "passed" if passed else "failed", "case_count": len(rows),
        "passed": sum(r["passed"] for r in rows), "acl_controls_passed": controls["passed"],
        "not_validated": ["broker restart or network partition", "TLS", "multiple concurrent source owners",
                          "production DiAgent", "invalid-message quarantine workflow", "stdio", "PostgreSQL"],
        "cases": [{"case": r["case"], "passed": r["passed"]} for r in rows]})
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
