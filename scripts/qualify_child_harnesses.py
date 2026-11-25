"""Opt-in mixed-Harness qualification using private operator profiles and synthetic input."""

import argparse
import asyncio
import copy
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / p)
    for p in ["backend", "sdk/python/src", "applications/static-review/src"]
]
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.delegations import Delegations
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.mcp_tool_transport import McpHttpTransport
from app.runtime.schema import attempts
from app.runtime.secret_provider import FileSecretMounts
from app.runtime.store import TERMINAL, RuntimeStore, digest
from app.runtime.tool_catalog import FileApprovers, FileToolCatalog
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.work_auth_schema import decisions
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.work_policy import FileWorkPolicy, permission
from app.runtime.worker import Worker, read_profile
from app.services.task_console import TaskConsole
from dios_review.app import sample
from qualify_harness_governance import SourceServer, save, wait_for
from sqlalchemy import select


def services(directory, scope):
    db = Database("sqlite+aiosqlite:///" + str(directory / "runtime.sqlite"))
    store = RuntimeStore(db, scope)
    provider = LocalArtifacts(directory / "objects")
    policy = FileWorkPolicy(directory / "policy.json")
    gate = ToolInvocations(
        store,
        policy,
        FileToolCatalog(directory / "tools.json"),
        provider,
        McpHttpTransport(),
        FileApprovers(directory / "approvers.json"),
    )
    return db, store, provider, policy, gate


def worker(directory, scope, profile_path, store, provider, policy, gate):
    raw = json.loads(profile_path.read_text())
    pid, spec = read_profile(profile_path)
    return Worker(
        store,
        DockerRuntimeAdapter(scope),
        pid,
        spec,
        provider,
        timeout=420,
        poll_interval=0.1,
        authorization_verifier=WorkAuthorizationVerifier(store, policy),
        secret_resolver=FileSecretMounts(directory / "secrets.json", scope),
        secret_requests=raw["secrets"],
        tool_gate=gate,
    )


async def run_worker(args):
    db, store, provider, policy, gate = services(args.output, args.scope)
    w = worker(args.output, args.scope, args.profile, store, provider, policy, gate)
    try:
        save(args.output / (w.profile_id + "-worker-result.json"), await w.once())
    finally:
        await db.close()


async def qualify(args):
    d = args.output.resolve()
    d.mkdir(mode=0o700, parents=True, exist_ok=False)
    scope = "mixed-child-" + uuid4().hex[:16]
    source = sample("first-item")
    mcp = SourceServer(source["source"], False)
    db = Database("sqlite+aiosqlite:///" + str(d / "runtime.sqlite"))
    await db.migrate()
    store = RuntimeStore(db, scope)
    provider = LocalArtifacts(d / "objects")
    child = json.loads((args.child_dir / "profile.json").read_text())
    child["launch"]["image"] = args.child_image
    if args.child_harness == "codex":
        for ref in child["secrets"]:
            if ref["target"] == "/tmp/auth.json":
                ref["target"] = "/run/secrets/codex-auth"
    save(
        d / "inputs.json",
        {"version": 1, "inputs": [source], "input_digest": "source-sha256"},
    )
    child["console"] = {
        "enabled": True,
        "name": args.child_harness + " child",
        "implementation": args.child_harness,
        "input_kind": "inline-static-review",
        "input_catalog": str(d / "inputs.json"),
        "synthetic": True,
    }
    (d / "profiles").mkdir()
    cp = d / "profiles/child.json"
    save(cp, child)
    cid, cspec = read_profile(cp)
    parent = json.loads((args.parent_dir / "profile.json").read_text())
    if args.parent_harness == "codex":
        for ref in parent["secrets"]:
            if ref["target"] == "/tmp/auth.json":
                ref["target"] = "/run/secrets/codex-auth"
    parent["id"] = args.parent_harness + "-parent"
    parent["launch"]["image"] = args.parent_image
    parent["launch"]["command"] = [
        "python",
        "/opt/dios/acp_child_coordinator.py",
        "--config",
        "/opt/dios/acp.json",
        "--harness",
        args.parent_harness,
    ]
    parent["tool_channel"] = {
        "tools": {},
        "acp_names": {
            "mcp__dios__dios_task_submit": "dios_task_submit",
            "mcp__dios__dios_task_get": "dios_task_get",
        },
        "children": {
            "profiles": {
                "delegate": {
                    "path": str(cp),
                    "profile_id": cid,
                    "profile_digest": cspec.fingerprint(),
                }
            },
            "max_children": 2,
            "max_depth": 2,
        },
    }
    value = {
        "id": "coordinate",
        "name": "Mixed Harness sample",
        "child_profile": "delegate",
        "child_input": source["id"],
    }
    value["sha256"] = digest(value)
    save(d / "parent-inputs.json", {"version": 1, "inputs": [value]})
    parent["console"] = {
        "enabled": True,
        "name": args.parent_harness + " coordinator",
        "implementation": args.parent_harness,
        "input_kind": "generic",
        "input_catalog": str(d / "parent-inputs.json"),
        "synthetic": True,
    }
    pp = d / "profiles/parent.json"
    save(pp, parent)
    pid, pspec = read_profile(pp)
    tools = json.loads((args.child_dir / "tools.json").read_text())
    tid = child["tool_channel"]["tools"]["repository_search"]
    tool = tools["tools"][tid]
    tool["url"] = f"http://127.0.0.1:{mcp.server.server_port}/mcp"
    tp = tool.pop("permission")
    tp["resource_version"] = digest({k: v for k, v in tool.items() if k != "enabled"})
    tool["permission"] = tp
    save(d / "tools.json", tools)
    config = {
        "version": 1,
        "policy_id": "mixed-harness",
        "subject": "operator:qualification",
        "actor": "service:harness",
        "profiles": {},
    }
    cd = {
        "profile_id": cid,
        "profile_digest": cspec.fingerprint(),
        "policy_id": config["policy_id"],
    }
    pd = {
        "profile_id": pid,
        "profile_digest": pspec.fingerprint(),
        "policy_id": config["policy_id"],
    }
    cperms = [permission(cd, "execute"), permission(cd, "accept_result"), tp]
    pperms = [
        permission(pd, "execute"),
        permission(pd, "accept_result"),
        permission(cd, "spawn"),
        *cperms,
    ]
    root = await Delegations(store).issue(
        "root",
        subject=config["subject"],
        actor=config["actor"],
        permissions=pperms,
        expires_at=time.time() + 3600,
        policy_evaluator=json.loads(args.policy_evaluator.read_text()),
    )
    registry = {"version": 1, "secrets": {}}
    for id, spec, raw, perms, src in [
        (pid, pspec, parent, pperms, args.parent_dir),
        (cid, cspec, child, cperms, args.child_dir),
    ]:
        config["profiles"][id] = {
            "profile_digest": spec.fingerprint(),
            "secret_refs": raw["secrets"],
            "work_ttl_seconds": 1800,
            "delegation_ref": root["id"],
            "permissions": {
                k: perms for k in ["caller", "service", "workload", "resource"]
            },
        }
        original = json.loads((src / "secrets.json").read_text())["secrets"]
        for ref in raw["secrets"]:
            key = ref["ref"]
            record = copy.deepcopy(original[key])
            record.update(scope=scope, profiles=[id])
            if key in registry["secrets"]:
                old = registry["secrets"][key]
                assert {k: v for k, v in old.items() if k != "profiles"} == {
                    k: v for k, v in record.items() if k != "profiles"
                }
                old["profiles"].append(id)
            else:
                registry["secrets"][key] = record
    save(d / "secrets.json", registry)
    save(d / "policy.json", config)
    token = secrets.token_urlsafe(32)
    save(
        d / "approvers.json",
        {
            "version": 1,
            "approvers": [
                {
                    "enabled": True,
                    "subject": "operator:independent-reviewer",
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "scopes": [scope],
                    "tools": [tid],
                }
            ],
        },
    )
    policy = FileWorkPolicy(d / "policy.json")
    gate = ToolInvocations(
        store,
        policy,
        FileToolCatalog(d / "tools.json"),
        provider,
        McpHttpTransport(),
        FileApprovers(d / "approvers.json"),
    )
    console = TaskConsole(db, scope, d / "profiles", d / "objects", policy=policy)
    processes = []
    run = None
    child_id = None

    async def launch(path):
        log = (d / (path.stem + "-worker.log")).open("w")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "worker",
            "--output",
            str(d),
            "--scope",
            scope,
            "--profile",
            str(path),
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        processes.append((proc, log))
        return proc

    async def terminal(run_id):
        current = await store.get(run_id)
        return current if current["state"] in TERMINAL else None

    adapter = DockerRuntimeAdapter(scope)
    try:
        run = await console.submit(
            pid,
            "coordinate",
            "静态检查空列表边界，返回有代码证据的成果。",
            "mixed-parent",
        )
        pw = await launch(pp)

        async def child_created():
            parent_run = await store.get(run["id"])
            if parent_run["state"] in TERMINAL:
                raise RuntimeError(
                    "Parent ended before child submission: " + parent_run["state"]
                )
            return (
                parent_run["family"]["children"][0]
                if parent_run["family"] and parent_run["family"]["children"]
                else None
            )

        child_id = (await wait_for(child_created, seconds=180))["run_id"]
        cw = await launch(cp)

        async def approval():
            current = await store.get(child_id)
            if current["state"] in TERMINAL:
                raise RuntimeError("Child ended before approval: " + current["state"])
            return next(
                (
                    x
                    for x in (await gate.list(child_id))["items"]
                    if x["state"] == "awaiting_approval"
                ),
                None,
            )

        call = await wait_for(approval, seconds=160)
        assert mcp.calls == 0
        pa = (await store.get(run["id"]))["attempts"][0]
        ca = (await store.get(child_id))["attempts"][0]
        assert pw.returncode is None and cw.returncode is None and pw.pid != cw.pid
        assert (
            pa["session_id"]
            and ca["session_id"]
            and pa["session_id"] != ca["session_id"]
        )
        assert (
            pa["process_id"]
            and ca["process_id"]
            and pa["process_id"] != ca["process_id"]
        )
        checks = [
            "separate-Worker-processes",
            "separate-Harness-processes-and-sessions",
            "approval-before-tool",
            "OPA-parent-child-intersection",
        ]
        if args.scenario == "success":
            await gate.decide(child_id, call["id"], call["version"], "approved", token)
            final_child = await wait_for(lambda: terminal(child_id), seconds=180)
            final_parent = await wait_for(lambda: terminal(run["id"]), seconds=180)
            assert final_child["state"] == final_parent["state"] == "succeeded"
            assert mcp.calls == 1
            detail = await console.detail(run["id"])
            artifact = next(a for a in detail["artifacts"] if a["kind"] == "result")
            blob = await console.artifact(run["id"], artifact["id"])
            result = json.loads(b"".join(provider.read(blob["storage_key"])))
            assert (
                result["child_run_id"] == child_id and result["child_result"]["summary"]
            )
            child_detail = await console.detail(child_id)
            child_artifact = next(
                a for a in child_detail["artifacts"] if a["kind"] == "result"
            )
            assert "sha256:" + child_artifact["digest"] in result["evidence"]
            checks += [
                "one-child-one-tool",
                "native-activity-linked",
                "verified-child-result-in-parent-report",
            ]
        else:
            if args.scenario == "parent-revoke":
                current = await store.get(run["id"])
                await store.revoke(run["id"], current["version"])
                checks += ["parent-revocation-cancels-child"]
            else:
                pw.kill()
                await pw.wait()
                assert (await adapter.observe(pa["id"])).alive
                # Isolated lease-expiry injection models an unrenewed owner lease; no task replay.
                async with db.transaction(scope) as c:
                    await c.execute(
                        attempts.update()
                        .where(attempts.c.id == pa["id"])
                        .values(lease_until=0)
                    )
                await store.expire()
                unknown = await store.get(run["id"])
                assert (
                    unknown["state"] == "reconciliation_required"
                    and len(unknown["attempts"]) == 1
                )
                checks += [
                    "killed-parent-Worker-live-workload-observed",
                    "expired-owner-cancels-child",
                    "unknown-result-not-redispatched",
                ]
            final_child = await wait_for(lambda: terminal(child_id), seconds=80)
            assert final_child["state"] == "cancelled" and mcp.calls == 0
            try:
                await gate.decide(
                    child_id, call["id"], call["version"], "approved", token
                )
            except RuntimeErrorCode:
                pass
            else:
                raise AssertionError("Approval revived a fenced child")
            if args.scenario == "parent-worker-loss":
                # Explicit operator termination closes the surviving process before recovery.
                current = await store.get(run["id"])
                await store.cancel(run["id"], current["version"])
                await adapter.stop(pa["id"])
                recover = worker(d, scope, pp, store, provider, policy, gate)
                await recover.recover()
                checks += ["operator-confirmed-exit-reconciled-with-original-attempt"]
            final_parent = await wait_for(lambda: terminal(run["id"]), seconds=80)
            assert (
                final_parent["state"] in {"failed", "cancelled"}
                and len(final_parent["attempts"]) == 1
            )
            checks += [
                "stale-approval-denied",
                "zero-tool-side-effects",
                "partial-output-retained",
            ]
        assert len((await store.get(run["id"]))["family"]["children"]) == 1
        parent_detail = await console.detail(run["id"])
        child_detail = await console.detail(child_id)
        for detail in [parent_detail, child_detail]:
            assert {"stdout", "stderr"} <= {
                artifact["kind"] for artifact in detail["artifacts"]
            }
        async with db.read(scope) as c:
            audit_rows = [
                dict(x)
                for x in (
                    await c.execute(
                        select(decisions).where(
                            decisions.c.run_id == run["id"],
                            decisions.c.action == "child-channel",
                        )
                    )
                ).mappings()
            ]
        evidence = {
            "passed": True,
            "parent": args.parent_harness,
            "child": args.child_harness,
            "scenario": args.scenario,
            "scope": scope,
            "parent_image": args.parent_image,
            "child_image": args.child_image,
            "worker_processes": [pw.pid, cw.pid],
            "checks": checks,
            "parent_run": parent_detail,
            "child_run": child_detail,
            "task_channel_calls": len(audit_rows),
            "tool_calls": mcp.calls,
        }
        save(d / "result.json", evidence)
        print(
            json.dumps(
                {
                    "passed": True,
                    "parent": args.parent_harness,
                    "child": args.child_harness,
                    "scenario": args.scenario,
                    "checks": checks,
                }
            ),
            flush=True,
        )
    finally:
        for proc, _ in processes:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except TimeoutError:
                    proc.terminate()
                    await proc.wait()
        for rid in [run["id"] if run else None, child_id]:
            if rid:
                current = await store.get(rid)
                if current["state"] not in TERMINAL:
                    await store.cancel(rid, current["version"])
                if current["attempt_id"]:
                    observation = await adapter.observe(current["attempt_id"])
                    if observation.alive:
                        await adapter.stop(current["attempt_id"])
        for path in [pp, cp]:
            recover = worker(d, scope, path, store, provider, policy, gate)
            await recover.recover()
        for _, log in processes:
            log.close()
        await db.close()
        await asyncio.to_thread(mcp.close)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("worker")
    w.add_argument("--scope", required=True)
    w.add_argument("--profile", type=Path, required=True)
    w.add_argument("--output", type=Path, required=True)
    q = sub.add_parser("qualify")
    q.add_argument("--output", type=Path, required=True)
    for side in ["parent", "child"]:
        q.add_argument("--" + side + "-dir", type=Path, required=True)
        q.add_argument(
            "--" + side + "-harness", choices=["cursor", "codex", "dsh"], required=True
        )
        q.add_argument("--" + side + "-image", required=True)
    q.add_argument("--policy-evaluator", type=Path, required=True)
    q.add_argument(
        "--scenario",
        choices=["success", "parent-revoke", "parent-worker-loss"],
        default="success",
    )
    args = parser.parse_args()
    asyncio.run(run_worker(args) if args.command == "worker" else qualify(args))


if __name__ == "__main__":
    main()
