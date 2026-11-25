"""Opt-in real Harness/OPA qualification in a fresh SQLite scope and Docker workload.

Uses an operator-owned profile and secret references. Only synthetic source is
provided to the model. Renewal/lease deadlines are injected in this isolated DB;
production policies, databases and running Workers are never updated.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "backend"),
    str(ROOT / "applications/static-review/src"),
    str(ROOT / "sdk/python/src"),
]


from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.delegations import Delegations
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.mcp_tool_transport import McpHttpTransport
from app.runtime.reconciliation import ExitReceipts
from app.runtime.schema import attempts
from app.runtime.secret_provider import FileSecretMounts
from app.runtime.store import RuntimeStore, digest
from app.runtime.tool_catalog import FileApprovers, FileToolCatalog
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.work_auth_schema import grants
from app.runtime.work_authorizations import WorkAuthorizationVerifier, view
from app.runtime.work_policy import FileWorkPolicy
from app.runtime.worker import Worker, read_profile
from app.services.task_inputs import CONSOLE_ORIGIN
from dios_review.app import sample


def save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)


class SourceServer:
    def __init__(self, source, hold):
        self.source = source
        self.calls = 0
        self.initializing = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    self.send_error(413)
                    return
                request = json.loads(self.rfile.read(size))
                if "id" not in request:
                    self.send_response(202)
                    self.end_headers()
                    return
                if request["method"] == "initialize":
                    owner.initializing.set()
                    if not owner.release.wait(15):
                        self.send_error(503)
                        return
                    result = {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "dios-governance-fixture",
                            "version": "1",
                        },
                    }
                elif request["method"] == "tools/call":
                    owner.calls += 1
                    actual = hashlib.sha256(owner.source.encode()).hexdigest()
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "filename": "sample.py",
                                        "sha256": actual,
                                        "source": owner.source,
                                    }
                                ),
                            }
                        ],
                        "isError": request["params"]["arguments"].get("query")
                        != actual,
                    }
                else:
                    result = {}
                raw = json.dumps(
                    {"jsonrpc": "2.0", "id": request["id"], "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


async def wait_for(check, *, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise TimeoutError("Qualification observation deadline")


async def qualify(args):
    directory = args.output.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    scope = "harness-" + uuid4().hex[:16]
    source = sample("first-item")
    positive_cases = {"renew", "approve", "native-deny", "recovery-crash"}
    server = SourceServer(source["source"], args.case not in positive_cases)
    db = Database("sqlite+aiosqlite:///" + str(directory / "runtime.sqlite"))
    worker_task = None
    worker = None
    run = None
    evidence = {"case": args.case, "scope": scope, "faults": [], "checks": []}
    try:
        await db.migrate()
        store = RuntimeStore(db, scope)
        provider = LocalArtifacts(directory / "objects")
        profile = json.loads(args.profile.read_text())
        if profile.get("governance", {}).get("mode") != "e2ag":
            raise ValueError("Qualification requires E2AG profile")
        if not profile["launch"]["image"].startswith("sha256:"):
            raise ValueError("Qualification requires an immutable Docker image ID")
        save(directory / "profile.json", profile)
        profile_id, spec = read_profile(directory / "profile.json")
        refs = profile.get("secrets", [])
        tool_id = profile["tool_channel"]["tools"]["repository_search"]
        registration = {
            "enabled": True,
            "transport": "mcp-http",
            "url": "http://127.0.0.1:" + str(server.server.server_port) + "/mcp",
            "name": "search",
            "approval_required": True,
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "result_schema": {
                "type": "object",
                "properties": {"content": {"type": "array"}},
                "required": ["content"],
            },
        }
        permission = {
            "action": "repository.search",
            "resource_kind": "mcp_tool",
            "resource_id": "sample/search",
            "resource_version": digest(
                {k: v for k, v in registration.items() if k != "enabled"}
            ),
            "constraint_revision": "harness-governance",
        }
        registration["permission"] = permission
        save(directory / "tools.json", {"version": 1, "tools": {tool_id: registration}})
        permissions = [
            {
                "action": "runtime." + action,
                "resource_kind": "runtime_profile",
                "resource_id": profile_id,
                "resource_version": spec.fingerprint(),
                "constraint_revision": "harness-governance",
            }
            for action in ("execute", "accept_result")
        ] + [permission]
        manager = Delegations(store)
        chain = []
        deadline = time.time() + 3600
        for depth in range(4):
            values = {
                "subject": "operator:qualification",
                "actor": "service:harness"
                if depth == 3
                else "service:delegate-" + str(depth),
                "permissions": permissions,
                "expires_at": deadline - depth * 30,
            }
            if depth:
                values.update(
                    parent_ref=chain[-1]["id"], issuer_actor=chain[-1]["actor_ref"]
                )
            else:
                values["policy_evaluator"] = json.loads(args.opa.read_text())
            chain.append(await manager.issue("level-" + str(depth), **values))
        evidence["delegation_chain"] = chain
        config = {
            "version": 1,
            "policy_id": "harness-governance",
            "subject": "operator:qualification",
            "actor": "service:harness",
            "profiles": {
                profile_id: {
                    "profile_digest": spec.fingerprint(),
                    "secret_refs": refs,
                    "work_ttl_seconds": 3600,
                    "delegation_ref": chain[-1]["id"],
                    "permissions": {
                        layer: permissions
                        for layer in ("caller", "service", "workload", "resource")
                    },
                }
            },
        }
        save(directory / "policy.json", config)
        policy = FileWorkPolicy(directory / "policy.json")
        registry = json.loads(args.secrets.read_text())
        selected = {}
        for ref in refs:
            selected[ref["ref"]] = dict(
                registry["secrets"][ref["ref"]], scope=scope, profiles=[profile_id]
            )
        save(directory / "secrets.json", {"version": 1, "secrets": selected})
        token = uuid4().hex
        save(
            directory / "approvers.json",
            {
                "version": 1,
                "approvers": [
                    {
                        "enabled": True,
                        "subject": "operator:independent-reviewer",
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "scopes": [scope],
                        "tools": [tool_id],
                    }
                ],
            },
        )
        gate = ToolInvocations(
            store,
            policy,
            FileToolCatalog(directory / "tools.json"),
            provider,
            McpHttpTransport(),
            FileApprovers(directory / "approvers.json"),
        )
        verifier = WorkAuthorizationVerifier(store, policy)
        worker = Worker(
            store,
            DockerRuntimeAdapter(scope),
            profile_id,
            spec,
            provider,
            timeout=240,
            poll_interval=0.1,
            authorization_verifier=verifier,
            secret_resolver=FileSecretMounts(directory / "secrets.json", scope),
            secret_requests=refs,
            tool_gate=gate,
        )
        run = await store.submit(
            "governance-" + args.case,
            {
                "origin": CONSOLE_ORIGIN,
                "task_kind": "inline-static-review",
                "display": {
                    "name": profile_id,
                    "implementation": "ACP governance qualification",
                    "synthetic": True,
                },
                "runtime_profile": profile_id,
                "profile_digest": spec.fingerprint(),
                "secret_refs": refs,
                "payload": {"sample": source, "goal": "检查空列表并引用检索证据"},
            },
            authorization=policy.admission(profile_id, spec.fingerprint(), refs),
        )
        evidence.update(run_id=run["id"], image=spec.image)
        if args.case == "recovery-crash":
            async def crash_before_commit(*_args, **_kwargs):
                current = await store.get(run["id"])
                receipt = ExitReceipts(provider, scope).load(current["attempts"][0])
                assert receipt and not receipt["observation"]["alive"]
                save(directory / "recovery-target.json", {
                    "run": current, "worker_pid": os.getpid(),
                    "tool_calls": server.calls, "scope": scope,
                    "evidence": evidence,
                })
                os._exit(75)
            store.complete = crash_before_commit
        worker_task = asyncio.create_task(worker.once())

        async def pending():
            if worker_task.done():
                await worker_task
                raise AssertionError("Harness finished before requesting approval")
            return next(
                (
                    x
                    for x in (await gate.list(run["id"]))["items"]
                    if x["state"] == "awaiting_approval"
                ),
                None,
            )

        item = await wait_for(pending, seconds=180)
        current = await store.get(run["id"])
        attempt = current["attempts"][-1]
        binding = (
            run["id"],
            attempt["id"],
            attempt["worker_id"],
            attempt["worker_instance"],
            attempt["epoch"],
        )
        observed = await worker.adapter.observe(attempt["id"])
        assert (
            observed.alive
            and observed.pid == attempt["process_id"]
            and attempt["session_id"]
        )
        assert server.calls == 0
        before = await view(store, run["id"])
        assert len(before["delegation"]["chain"]) == 4
        evidence.update(
            attempt_id=attempt["id"],
            session_id=attempt["session_id"],
            process_id=attempt["process_id"],
            authorization_before=before,
            invocation_id=item["id"],
        )
        evidence["checks"].append("real-process-session-and-three-level-delegation")
        if getattr(args, "release_file", None):
            save(directory / "ready.json", {
                "worker_pid": os.getpid(), "worker_id": worker.worker_id,
                "run_id": run["id"], "attempt_id": attempt["id"],
                "session_id": attempt["session_id"], "process_id": observed.pid,
                "process_instance": observed.process_instance, "scope": scope,
                "observed_at": time.time(),
            })
            async def released():
                if worker_task.done():
                    await worker_task
                    raise AssertionError("Worker ended before concurrency barrier")
                return args.release_file.is_file()
            await wait_for(released, seconds=90)
        if args.case == "approval-expired":
            async with db.read(scope) as c:
                original = await gate._row(c, run["id"], item["id"])
                request_key = original["request_key"]
            try:
                await gate.request(run["id"], attempt["id"], attempt["session_id"],
                    tool_id, {"query": "changed-synthetic-argument"}, request_key)
            except RuntimeErrorCode as exc:
                assert exc.code == "IDEMPOTENCY_CONFLICT", exc.code
            else:
                raise AssertionError("Changed arguments reused the original request")
            assert server.calls == 0
            evidence["checks"].append("changed-arguments-cannot-reuse-real-pending-invocation")
        if args.case == "renew":
            async with db.transaction(scope) as c:
                shortened = await db.now(c) + 100
                await c.execute(
                    grants.update()
                    .where(grants.c.attempt_id == attempt["id"])
                    .values(expires_at=shortened)
                )
            evidence["faults"].append(
                {"kind": "isolated-grant-deadline-shortened", "expires_at": shortened}
            )

            async def renewed():
                value = await view(store, run["id"])
                return value if value["grant"]["expires_at"] > shortened else None

            renewed_view = await wait_for(renewed)
            assert renewed_view["grant"]["expires_at"] <= renewed_view["expires_at"]
            assert renewed_view["grant"]["version"] > before["grant"]["version"]
            assert any(
                "EXECUTION_GRANT_RENEWED" in row["reason_codes"]
                for row in renewed_view["decisions"]
            )
            evidence["authorization_renewed"] = renewed_view
            evidence["checks"].append(
                "worker-heartbeat-renews-current-grant-within-delegated-deadline"
            )
        approved = await gate.decide(
            run["id"], item["id"], item["version"], "approved", token
        )
        evidence["approved_version"] = approved["version"]
        if args.case == "approval-expired":
            try:
                await gate.decide(run["id"], item["id"], item["version"], "approved", token)
            except RuntimeErrorCode as exc:
                assert exc.code == "VERSION_CONFLICT", exc.code
            else:
                raise AssertionError("Stale approval was accepted")
            evidence["checks"].append("stale-approval-replay-rejected")
        if args.case not in positive_cases:

            async def initializing():
                return server.initializing.is_set()

            await wait_for(initializing, seconds=10)
            assert server.calls == 0
            if args.case == "ancestor-revoke":
                await manager.revoke(chain[0]["id"], chain[0]["version"])
                expected = "AUTH_REVOKED"
                evidence["faults"].append(
                    {"kind": "root-delegation-revoked", "reference": chain[0]["id"]}
                )
            elif args.case == "approval-expired":
                from app.runtime.tool_schema import invocations
                async with db.transaction(scope) as c:
                    await c.execute(invocations.update().where(invocations.c.id == item["id"]).values(expires_at=0))
                expected = "TOOL_AUTHORIZATION_EXPIRED"
                evidence["faults"].append({"kind":"isolated-invocation-expired","invocation_id":item["id"]})
            else:
                async with db.transaction(scope) as c:
                    await c.execute(
                        attempts.update()
                        .where(attempts.c.id == attempt["id"])
                        .values(lease_until=0)
                    )
                assert await store.expire() == 1
                fenced = await store.get(run["id"])
                assert fenced["epoch"] == attempt["epoch"] + 1
                expected = "STALE_OWNER"
                evidence["faults"].append(
                    {"kind": "isolated-lease-expired", "new_epoch": fenced["epoch"]}
                )
            if args.case != "approval-expired":
                try:
                    await store.heartbeat(*binding)
                except RuntimeErrorCode as exc:
                    assert exc.code == expected, exc.code
                    evidence["old_owner_error"] = exc.code
                else:
                    raise AssertionError("Old authority renewed")
            server.release.set()
        try:
            await asyncio.wait_for(asyncio.shield(worker_task), 120)
        except RuntimeErrorCode as exc:
            if args.case != "stale-epoch" or exc.code != "STALE_OWNER":
                raise
            evidence["worker_error"] = exc.code
        if args.case == "stale-epoch":
            replacement = Worker(
                store,
                worker.adapter,
                profile_id,
                spec,
                provider,
                authorization_verifier=verifier,
                tool_gate=gate,
            )
            await replacement.recover()
        final = await store.get(run["id"])
        assert (
            len(final["attempts"]) == 1
            and final["attempts"][0]["session_id"] == attempt["session_id"]
        )
        invocation = await gate.get(run["id"], item["id"])
        evidence.update(
            state=final["state"],
            attempt_error=final["attempts"][0]["error"],
            tool_calls=server.calls,
            invocation=invocation,
            authorization_after=await view(store, run["id"]),
        )
        receipts = ExitReceipts(provider, scope)
        receipt = receipts.load(final["attempts"][0])
        assert receipt and not receipt["observation"]["alive"]
        frames = [
            json.loads(x)
            for x in b"".join(provider.read(receipt["stdout"])).splitlines()
            if x.startswith(b"{")
        ]
        evidence["result_frames"] = sum(x.get("type") == "result" for x in frames)
        usage_frames = [x for x in frames if x.get("type") == "usage"]
        model_usage = final["model_usage"]
        assert model_usage["evidence_level"] == "agent_reported"
        assert model_usage["settlement"] == "unreconciled"
        latest_usage = {x["kind"]: x for x in usage_frames}
        assert len(model_usage["items"]) == len(latest_usage)
        for item in model_usage["items"]:
            source_usage = latest_usage[item["kind"]]
            assert item["attempt_id"] == attempt["id"]
            assert item["session_id"] == attempt["session_id"]
            for key in ("sequence", "quality", "values"):
                assert item[key] == source_usage[key]
            assert item["evidence"].get("artifact_key")
        evidence["usage"] = {
            "frame_count": len(usage_frames),
            "availability": "reported" if usage_frames else "not_reported",
            "model_usage": model_usage,
        }
        evidence["checks"].append("usage-matches-retained-output-or-remains-unknown")
        evidence["exit_receipt_sha256"] = hashlib.sha256(
            receipts.path(attempt["id"]).read_bytes()
        ).hexdigest()
        evidence["exit"] = receipt["observation"]
        if args.case in {"renew", "approve"}:
            assert (
                final["state"] == "succeeded"
                and server.calls == 1
                and evidence["result_frames"] == 1
            )
            again = await gate.dispatch(
                run["id"], invocation["id"], invocation["version"]
            )
            assert again["state"] == "succeeded" and server.calls == 1
            evidence["checks"].append(
                "completed-dispatch-replay-does-not-call-tool-again"
            )
        elif args.case == "native-deny":
            assert final["state"] == "failed" and server.calls == 1
            assert evidence["result_frames"] == 0
            assert invocation["state"] == "succeeded"
            evidence["checks"].append("unverified-native-activity-cannot-be-accepted-as-success")
        else:
            assert (
                final["state"] == "failed"
                and server.calls == 0
                and evidence["result_frames"] == 0
            )
            assert invocation["state"] == "failed"
            assert invocation["reason"] == "TOOL_NOT_DISPATCHED_RUN_TERMINAL"
            assert any(
                (
                    row["stage"] in {"dispatch", "preflight"}
                    and expected in row["decision"]["reason_codes"]
                    or row["stage"] == "lifecycle"
                    and "TOOL_NOT_DISPATCHED_RUN_TERMINAL"
                    in row["decision"]["reason_codes"]
                )
                and row["decision"]["decision"] == "deny"
                for row in invocation["evidence"]
            ), "No execution gate denial recorded"
            evidence["checks"].append("approved-tool-blocked-after-authority-change")
            evidence["checks"].append(
                "unstarted-tool-closed-with-original-approval-retained"
            )
        evidence["checks"].append("original-run-attempt-session-retained")
        evidence["passed"] = True
        save(directory / "result.json", evidence)
        print(
            json.dumps(
                {
                    k: evidence[k]
                    for k in (
                        "case",
                        "run_id",
                        "state",
                        "tool_calls",
                        "checks",
                        "passed",
                    )
                }
            )
        )
    finally:
        server.release.set()
        if worker_task and not worker_task.done():
            current = await store.get(run["id"])
            if current["state"] not in {"succeeded", "failed", "cancelled"}:
                await store.cancel(run["id"], current["version"])
            try:
                await asyncio.wait_for(asyncio.shield(worker_task), 30)
            except (TimeoutError, RuntimeErrorCode):
                await worker.adapter.stop((await store.get(run["id"]))["attempt_id"])
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
        save(directory / "observations.json", evidence)
        await db.close()
        await asyncio.to_thread(server.close)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("profile", "secrets", "opa", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--release-file", type=Path)
    parser.add_argument(
        "--case", required=True, choices=("renew", "approve", "native-deny", "approval-expired", "ancestor-revoke", "stale-epoch", "recovery-crash")
    )
    args = parser.parse_args()
    asyncio.run(qualify(args))


if __name__ == "__main__":
    main()
