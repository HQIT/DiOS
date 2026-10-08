"""Isolated process-restart qualification with native governance and HTTP tool.

The task-container launcher is replaced by a deterministic executor. No model,
production service or public repository is used. The PEP and upstream HTTP
client are not mocked. This is not a DiAgent container deployment experiment.
"""
import argparse
import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from contextlib import nullcontext
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tmp/heterogeneity-deps"))
from file_ingress import read_ready_event
from durable_ingress import accept_event, metadata, receipts, conflicts

CUTS = {"before_receive_commit": 80, "after_receive_commit": 81,
        "before_dispatch": 82, "after_dispatch_commit": 83,
        "after_handoff_commit": 84, "during_task_launch": 85}


def save_new(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def append_record(path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def tool_endpoint(directory):
    operations = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = payload.get("method")
            if method == "tools/call":
                item = {"method": method, **payload["params"]}
                operations.append(item)
                append_record(directory / "tool-receipts.jsonl", item)
                # Permissive endpoint: any request reaching here is observable;
                # do not hide PEP failures behind an upstream access check.
                append_record(directory / "artifacts.jsonl", payload["params"])
                result = {"content": [{"type": "text", "text": "stored"}]}
            elif method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "isolated-receipt", "version": "1"}}
            elif method == "notifications/initialized":
                self.send_response(202); self.end_headers(); return
            else:
                self.send_response(400); self.end_headers(); return
            raw = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)

        def do_GET(self):
            self.send_response(405); self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", operations
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


async def worker(args):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.config import settings
    from app.connectors import registry
    from app.connectors.contracts import ConnectorManifest, EventTypeDecl
    from app.db.database import Base, get_db
    from app.models.tables import Agent, Subscription, McpServer, EventLog, A2ATask, E2AGApproval, E2AGToolGrant, E2AGToolInvocation
    from app.services.e2ag import verify_audit_chain
    from app.services.e2ag_tool_gateway import issue_task_mcp_config, revoke_task_grants
    from app.services.e2ag_approval import decide_approval
    from app.api.internal.e2ag_mcp import router
    from app.services.e2ag_stdio_transport import stdio_transport_lifespan
    from fastapi import FastAPI
    import httpx

    folder = args.directory.resolve()
    database_url = os.environ.get("E2AG_QUAL_DATABASE_URL") or "sqlite+aiosqlite:///" + (folder / "state.db").as_posix()
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings.e2ag_mode = "enforce"
    settings.event_dedup_enabled = True
    tool_transport = getattr(args, "tool_transport", "streamable_http")
    settings.e2ag_stdio_server_ids = ["file-tool"] if tool_transport == "stdio" else []
    ingress = getattr(args, "ingress", "file")
    source, event_type = f"{ingress}/fixture", f"{ingress}.received"
    registry.all_manifests()
    registry.register(ConnectorManifest(type=f"{ingress}-qualification", label="Ingress qualification",
        accepted_source_patterns=(source,), event_types=(EventTypeDecl(event_type),)))
    def checkpoint(stage):
        if stage == args.cut:
            os._exit(CUTS[stage])

    async def fixed_executor(db, agent, task):
        checkpoint("during_task_launch")
        task.status = "working"
        await db.commit()
        server = await db.get(McpServer, "file-tool")
        config, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
        if omissions:
            raise RuntimeError(str(omissions))
        conn = config["receipt"]
        grant_id = conn["url"].rsplit("/", 1)[-1]
        app = FastAPI()
        app.include_router(router, prefix="/api")
        async def scoped_db():
            if args.mode == "revoked_fresh":
                async with sessions() as fresh:
                    yield fresh
            else:
                yield db
        app.dependency_overrides[get_db] = scoped_db
        path = f"/api/internal/e2ag/mcp/{grant_id}"
        headers = {**conn["headers"], "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-06-18"}
        async with stdio_transport_lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qualification", trust_env=False) as client:
            init = await client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": 1,
                "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "fixed-executor", "version": "1"}}})
            if init.status_code != 200:
                raise RuntimeError("MCP initialization failed")
            notice = await client.post(path, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            if notice.status_code != 202:
                raise RuntimeError("MCP initialized notification failed")
            grant = await db.get(E2AGToolGrant, grant_id)
            if args.mode == "revoked_fresh":
                async with sessions() as writer:
                    await revoke_task_grants(writer, task.id)
            elif args.mode in {"revoked", "revoked_reconnect"}:
                await revoke_task_grants(db, task.id)
                await db.commit()
                if args.mode == "revoked_reconnect":
                    await app.state.e2ag_stdio_pool.close_session(grant_id)
                    reconnect = await client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": 3,
                        "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "fixed-executor-reconnect", "version": "1"}}})
                    append_record(folder / "reconnect.jsonl", {"grant_id": grant_id,
                        "revocation_committed": True, "old_session_closed": True,
                        "reinitialize_status": reconnect.status_code, "reinitialize_response": reconnect.json()})
            elif args.mode == "expired":
                grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
            event = await db.get(EventLog, task.context_id)
            arguments = {"resource": "report/other" if args.mode == "resource" else "report/allowed",
                         "report": event.cloud_event["data"]["text"].upper()}
            response = await client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": 2,
                "method": "tools/call", "params": {"name": "other.write" if args.mode == "tool" else "report.write",
                "arguments": arguments}})
        # Preserve native lifecycle facts, without calling denied work complete.
        task.artifacts = [{"qualification_call_status": response.status_code,
                           "response": response.json()}]
        task.status = "completed" if response.status_code == 200 else "failed"
        await revoke_task_grants(db, task.id)
        await db.commit()

    try:
        if args.op == "setup":
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.run_sync(metadata.create_all)
            async with sessions() as db:
                db.add(Agent(id="file-agent", name="Fixed file workflow", mode="task", capabilities={"governance": {
                    "allowed_event_sources": [f"{ingress}/other" if args.mode == "deny" else source],
                    "allowed_event_types": [event_type], "allowed_actions": ["report.create", "secret.read"],
                    "require_action_declaration": True, "allowed_tools": ["report.write"],
                    "argument_constraints": {"version": 2, "tools": {"report.write": {
                        "resource": {"type": "string", "enum": ["report/allowed"]},
                        "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}}}))
                db.add(Subscription(agent_id="file-agent", source_pattern=f"{ingress}/*", event_types=[event_type]))
                db.add(McpServer(id="file-tool", name="receipt", transport=tool_transport, url=args.url if tool_transport != "stdio" else "",
                    command=sys.executable if tool_transport == "stdio" else "",
                    args=[str(Path(__file__).with_name("stdio_receipt_tool.py")), "--directory", str(folder)] if tool_transport == "stdio" else []))
                await db.commit()
            return {"setup": True}
        if args.op == "approval_race":
            from sqlalchemy.sql.dml import Update
            gate, arrived = asyncio.Event(), 0
            async def contender(actor):
                nonlocal arrived
                async with sessions() as own:
                    approval = (await own.execute(select(E2AGApproval))).scalar_one()
                    original = own.execute
                    async def gated_execute(statement, *values, **kwargs):
                        nonlocal arrived
                        if isinstance(statement, Update) and statement.table.name == "e2ag_approvals":
                            arrived += 1
                            if arrived == 2: gate.set()
                            await asyncio.wait_for(gate.wait(), 10)
                        return await original(statement, *values, **kwargs)
                    try:
                        with patch.object(own, "execute", gated_execute):
                            await decide_approval(own, approval.id, decision="approved", actor=actor)
                        return {"approval": "approved"}
                    except ValueError as exc:
                        return {"approval_error": str(exc)}
            with patch("app.services.a2a_service._send_to_task_agent", fixed_executor):
                outcomes = await asyncio.wait_for(asyncio.gather(contender("reviewer-1"), contender("reviewer-2")), 30)
            return {"outcomes": outcomes, "synchronized_claims": arrived}
        async with sessions() as db:
            if args.op == "consume":
                if ingress == "http":
                    from http_ingress import receive_one
                    async def consume_http(event):
                        with patch("app.services.a2a_service._send_to_task_agent", fixed_executor):
                            return await accept_event(event, db, recover_interrupted=args.recover, checkpoint=checkpoint)
                    return await receive_one(consume_http, folder, os.environ["E2AG_HTTP_SECRET"])
                if ingress == "amqp":
                    from amqp_ingress import BrokerDelivery
                    delivery_context = BrokerDelivery(args.broker_port, args.queue)
                else:
                    delivery_context = nullcontext(None)
                with delivery_context as delivery, patch("app.services.a2a_service._send_to_task_agent", fixed_executor):
                    event = delivery.event() if delivery else read_ready_event(folder, "input.ready.json", "fixture")
                    disposition = await accept_event(event, db, recover_interrupted=args.recover,
                                                     checkpoint=checkpoint)
                    if delivery:
                        if disposition["acknowledge"]:
                            delivery.acknowledge()
                        disposition["broker"] = {"redelivered": delivery.method.redelivered,
                            "delivery_tag": delivery.method.delivery_tag, "acknowledged": delivery.acknowledged,
                            "message_id": delivery.properties.message_id}
                return disposition
            if args.op == "approve":
                approval = (await db.execute(select(E2AGApproval))).scalar_one()
                try:
                    with patch("app.services.a2a_service._send_to_task_agent", fixed_executor):
                        await decide_approval(db, approval.id, decision="approved", actor="qualification-reviewer")
                    return {"approval": "approved"}
                except ValueError as exc:
                    return {"approval_error": str(exc)}
            snapshot = {}
            for key, model in [("events", EventLog), ("tasks", A2ATask), ("approvals", E2AGApproval),
                               ("grants", E2AGToolGrant), ("invocations", E2AGToolInvocation)]:
                rows = (await db.execute(select(model))).scalars().all()
                snapshot[key] = [{col.name: (value.isoformat() if isinstance(value, datetime) else value)
                    for col in model.__table__.columns if col.name != "token_hash"
                    for value in [getattr(row, col.name)]} for row in rows]
            snapshot["receipts"] = [dict(row) for row in (await db.execute(select(receipts))).mappings()]
            snapshot["conflicts"] = [dict(row) for row in (await db.execute(select(conflicts))).mappings()]
            snapshot["audit_valid"] = all(verify_audit_chain(row["audit_chain"]) for row in snapshot["events"])
            return snapshot
    finally:
        await engine.dispose()


def run_case(folder, mode, cut=None, changed_content=False, tool_transport="streamable_http", database_url=None, approval_race=False, ingress="file"):
    folder.mkdir()
    body = {"id": "delivery-1", "subject": "Qualification task",
            "data": {"text": "governance receipt", "environment": "production",
                     "requested_action": "secret.read" if mode in {"pending", "approved"} else "report.create"}}
    save_new(folder / "input.ready.json", body)
    process_records = []
    race = None
    import secrets
    http_secret = secrets.token_urlsafe(24)
    def invoke(op, url, **options):
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--directory", str(folder),
                   "--op", op, "--mode", mode, "--url", url, "--tool-transport", tool_transport, "--ingress", ingress]
        if options.get("recover"): command.append("--recover")
        if options.get("cut"): command += ["--cut", options["cut"]]
        environment = dict(os.environ)
        if database_url:
            environment["E2AG_QUAL_DATABASE_URL"] = database_url
        else:
            environment.pop("E2AG_QUAL_DATABASE_URL", None)
        if ingress == "http" and op == "consume":
            from http_ingress import invoke_http
            environment["E2AG_HTTP_SECRET"] = http_secret
            proc = invoke_http(command, cwd=ROOT, environment=environment, folder=folder,
                               raw=json.dumps(body).encode(), secret=http_secret)
        else:
            proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=40, env=environment)
        entry = {"op": op, "exit_code": proc.returncode, "stderr": proc.stderr, "stdout": proc.stdout}
        process_records.append(entry)
        append_record(folder / "processes.jsonl", entry)
        if options.get("cut"):
            assert proc.returncode == CUTS[options["cut"]], entry
            return {}
        assert proc.returncode == 0, entry
        value = json.loads(proc.stdout)
        assert "worker_error" not in value, value
        return value
    with tool_endpoint(folder) as (url, operations):
        invoke("setup", url)
        first = invoke("consume", url, cut=cut)
        if cut:
            # subprocess.run has returned a terminal exit code: the former
            # owner is proven stopped before takeover in a new process.
            first = invoke("consume", url, recover=True)
        if mode == "approved":
            if approval_race:
                race = invoke("approval_race", url)
                assert race["synchronized_claims"] == 2, race
                assert race["outcomes"].count({"approval": "approved"}) == 1, race
                assert race["outcomes"].count({"approval_error": "APPROVAL_ALREADY_DECIDED"}) == 1, race
            else:
                assert invoke("approve", url) == {"approval": "approved"}
            assert invoke("approve", url) == {"approval_error": "APPROVAL_ALREADY_DECIDED"}
        second = invoke("consume", url)
        conflict = None
        if changed_content:
            body["data"]["text"] = "changed content"
            # Publish a distinct conflicting delivery; do not mutate an open
            # ready file. The previous reader process has already terminated.
            save_new(folder / "replacement.tmp", body)
            (folder / "replacement.tmp").replace(folder / "input.ready.json")
            conflict = invoke("consume", url)
        snapshot = invoke("snapshot", url)
    if tool_transport == "stdio":
        log = folder / "tool-receipts.jsonl"
        operations = [json.loads(s) for s in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    unresolved = cut == "during_task_launch"
    expected_tools = 1 if mode in {"normal", "approved"} and not unresolved else 0
    expected_tasks = 0 if mode in {"deny", "pending"} else 1
    checks = {
        "native_task_count": len(snapshot["tasks"]) == expected_tasks,
        "native_event_count": len(snapshot["events"]) == 1,
        "tool_receipts": len(operations) == expected_tools,
        "audit_hashes": snapshot["audit_valid"],
        "repeat_handled": second["disposition"] == ("in_progress" if unresolved else "duplicate"),
        "recovery_result": first["disposition"] == ("native_state_unresolved" if unresolved else "recorded" if cut != "after_handoff_commit" else "duplicate"),
        "conflict_record": (conflict is None or (conflict["disposition"] == "conflict" and len(snapshot["conflicts"]) == 1)),
        "artifact_content": all(op["name"] == "report.write" and op["arguments"] == {
            "resource": "report/allowed", "report": "GOVERNANCE RECEIPT"} for op in operations),
    }
    if mode in {"tool", "resource", "revoked", "expired", "revoked_fresh"}:
        checks["explicit_pep_denial"] = snapshot["tasks"][0]["artifacts"][0]["qualification_call_status"] == 403
    if mode == "pending":
        checks["approval_pending"] = len(snapshot["approvals"]) == 1 and snapshot["approvals"][0]["status"] == "pending"
    if unresolved:
        checks["unresolved_not_acknowledged"] = not first["acknowledge"] and not second["acknowledge"]
    return {"case": folder.name, "mode": mode, "cut": cut, "tool_transport": tool_transport, "approval_race": race, "first": first, "repeat": second,
            "conflict": conflict, "checks": checks, "passed": all(checks.values()),
            "snapshot": snapshot, "tool_operations": operations, "processes": process_records,
            "business_complete": expected_tools == 1 and len(operations) == 1}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--op", choices=["setup", "consume", "approve", "approval_race", "snapshot"])
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--url", default="")
    parser.add_argument("--ingress", choices=["file", "amqp", "http"], default="file")
    parser.add_argument("--tool-transport", choices=["streamable_http", "stdio"], default="streamable_http")
    parser.add_argument("--broker-port", type=int)
    parser.add_argument("--queue")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--cut", choices=list(CUTS))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(asyncio.run(worker(args)), ensure_ascii=False))
        return
    if args.output is None or args.output.exists():
        raise SystemExit("A fresh output directory is required")
    output = args.output.resolve()
    output.mkdir(parents=True)
    sources = sorted((ROOT / "backend/app").rglob("*.py")) + sorted(Path(__file__).parent.glob("*.py"))
    save_new(output / "freeze.json", {"kind": "qualification", "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "native send/admission/grant/PEP; deterministic container-launch replacement; ASGI proxy ingress; real loopback HTTP tool; process restarts; no model",
        "source_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    rows = []
    cases = [(mode, mode, None, False) for mode in ["normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"]]
    cases += [("identity_conflict", "normal", None, True)]
    cases += [(cut, "normal", cut, False) for cut in CUTS]
    def execute_case(case):
        name, mode, cut, changed = case
        try:
            row = run_case(output / name, mode, cut, changed)
        except Exception:
            import traceback
            row = {"case": name, "passed": False, "error": traceback.format_exc()}
        return row
    # Independent DBs/ports only; these timings are not performance evidence.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(execute_case, case) for case in cases]):
            row = future.result()
            save_new(output / (row["case"] + ".json"), row)
            rows.append(row)
            print(json.dumps({"case": row["case"], "passed": row["passed"], "checks": row.get("checks"), "error": row.get("error")}), flush=True)
    save_new(output / "summary.json", {"status": "passed" if all(r["passed"] for r in rows) else "failed",
        "case_count": len(rows), "passed": sum(r["passed"] for r in rows),
        "not_validated": ["DiAgent container", "model generalization", "all crash interleavings", "AMQP", "stdio", "PostgreSQL"],
        "unresolved_case": "during_task_launch deliberately requires reconciliation; no automatic replay or business completion",
        "cases": [{"case": r["case"], "passed": r["passed"]} for r in rows]})
    raise SystemExit(0 if all(r["passed"] for r in rows) else 1)


if __name__ == "__main__":
    main()
