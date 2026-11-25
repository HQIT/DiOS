"""Opt-in release qualification with a real ACP Harness and synthetic read-only input."""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / p)
    for p in ("backend", "applications/static-review/src", "sdk/python/src")
]

from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database
from app.runtime.deployments import Deployments
from app.runtime.execution_revisions import load_profile, publish_profile
from app.runtime.mcp_tool_transport import McpHttpTransport
from app.runtime.schema import artifacts
from app.runtime.store import RuntimeStore, digest
from app.runtime.tool_catalog import FileApprovers, FileToolCatalog
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.work_policy import FileWorkPolicy
from app.services.task_console import TaskConsole
from dios_review.app import sample
from qualify_harness_governance import SourceServer, save
from sqlalchemy import select


async def qualify(args):
    root = args.output.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    scope = "revision-release-" + uuid4().hex[:16]
    db = Database("sqlite+aiosqlite:///" + str(root / "runtime.sqlite"))
    source = sample("first-item")
    server = SourceServer(source["source"], False)
    children = []
    logs = []
    refs = {}
    specs = {}
    report = {"scope": scope, "runs": {}, "checks": []}
    try:
        await db.migrate()
        store = RuntimeStore(db, scope)
        provider = LocalArtifacts(root / "objects")
        base = json.loads(args.profile.read_text())
        profile_id = base["id"]
        assert base["governance"]["mode"] == "e2ag" and base["launch"][
            "image"
        ].startswith("sha256:")
        # Copy only controlled non-secret input, leaving the original harness installation intact.
        for index, mount in enumerate(base["launch"].get("mounts", [])):
            assert mount["read_only"] and Path(mount["source"]).is_file()
            target = root / ("controlled-" + str(index) + ".json")
            shutil.copyfile(mount["source"], target)
            mount["source"] = str(target)
        base["console"] = {
            "enabled": True,
            "name": "Release qualification",
            "description": "Synthetic read-only source",
            "implementation": "Cursor ACP",
            "synthetic": True,
            "input_kind": "inline-static-review",
            "input_catalog_data": {
                "version": 1,
                "inputs": [source],
                "input_digest": "source-sha256",
                "requires_e2ag": True,
            },
        }
        for name in ("old", "new", "bad"):
            raw = copy.deepcopy(base)
            raw["launch"].setdefault("environment", {})["DIOS_RELEASE_LABEL"] = name
            if name == "bad":
                raw["launch"]["command"] = ["python", "-c", "raise SystemExit(7)"]
            path = root / (name + ".json")
            save(path, raw)
            row = await publish_profile(db, scope, path, name, "qualification")
            ref = row["revision"]["id"]
            _, _, spec, _, _ = await load_profile(db, scope, ref)
            refs[name] = ref
            specs[name] = spec
        # Subsequent source edits and deletion cannot change any published Revision.
        edited = copy.deepcopy(base)
        edited["launch"]["command"] = ["python", "-c", "raise SystemExit(99)"]
        save(root / "old.json", edited)
        for mount in base["launch"].get("mounts", []):
            Path(mount["source"]).unlink()
        tool_id = base["tool_channel"]["tools"]["repository_search"]
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
            "constraint_revision": "revision-release",
        }
        registration["permission"] = permission
        save(root / "tools.json", {"version": 1, "tools": {tool_id: registration}})
        rows = {}
        for name, spec in specs.items():
            permissions = [
                {
                    "action": "runtime." + action,
                    "resource_kind": "runtime_profile",
                    "resource_id": profile_id,
                    "resource_version": spec.fingerprint(),
                    "constraint_revision": "revision-release",
                }
                for action in ("execute", "accept_result")
            ] + [permission]
            rows[spec.fingerprint()] = {
                "profile_digest": spec.fingerprint(),
                "secret_refs": base.get("secrets", []),
                "work_ttl_seconds": 3600,
                "permissions": {
                    layer: permissions
                    for layer in ("caller", "service", "workload", "resource")
                },
            }
        policy_value = {
            "version": 1,
            "policy_id": "revision-release",
            "subject": "operator:qualification",
            "actor": "service:release",
            "profiles": {
                profile_id: {**rows[specs["new"].fingerprint()], "revisions": rows}
            },
        }
        save(root / "policy.json", policy_value)
        policy = FileWorkPolicy(root / "policy.json")
        registry = json.loads(args.secrets.read_text())
        selected = {
            ref["ref"]: {
                **registry["secrets"][ref["ref"]],
                "scope": scope,
                "profiles": [profile_id],
            }
            for ref in base.get("secrets", [])
        }
        save(root / "secrets.json", {"version": 1, "secrets": selected})
        token = uuid4().hex
        save(
            root / "approvers.json",
            {
                "version": 1,
                "approvers": [
                    {
                        "enabled": True,
                        "subject": "operator:qualification-approver",
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
            FileToolCatalog(root / "tools.json"),
            provider,
            McpHttpTransport(),
            FileApprovers(root / "approvers.json"),
        )
        deployment = Deployments(db, scope, policy)
        profiles = root / "profiles"
        profiles.mkdir()
        save(
            profiles / "release.json",
            {"id": profile_id, "deployment_id": "primary", "enabled": True},
        )
        console = TaskConsole(db, scope, profiles, provider.root, policy=policy)
        console.tools = gate

        def start(name):
            log = (root / ("worker-" + str(len(children)) + ".log")).open("w")
            logs.append(log)
            command = [
                sys.executable,
                "-m",
                "app.runtime.cli",
                "--database-url",
                db.engine.url.render_as_string(hide_password=False),
                "--scope",
                scope,
                "worker",
                "--revision-ref",
                refs[name],
                "--policy-file",
                str(root / "policy.json"),
                "--secrets-file",
                str(root / "secrets.json"),
                "--tools-file",
                str(root / "tools.json"),
                "--approvers-file",
                str(root / "approvers.json"),
                "--artifacts",
                str(provider.root),
                "--once",
                "--timeout",
                "240",
                "--poll-interval",
                ".1",
            ]
            child = subprocess.Popen(
                command,
                cwd=ROOT / "backend",
                env=dict(os.environ, PYTHONPATH=str(ROOT / "backend")),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            children.append(child)
            return child

        async def wait(run_id, child, *, approve=True, stop_at_approval=False):
            deadline = asyncio.get_running_loop().time() + 300
            while asyncio.get_running_loop().time() < deadline:
                run = await store.get(run_id)
                if run["state"] in {"succeeded", "failed", "cancelled"}:
                    return run
                for item in (await gate.list(run_id))["items"]:
                    if item["state"] == "awaiting_approval":
                        if stop_at_approval:
                            return run
                        if approve:
                            await gate.decide(
                                run_id,
                                item["id"],
                                item["version"],
                                "approved",
                                token,
                                "Synthetic release qualification",
                            )
                if child.poll() is not None:
                    raise AssertionError(
                        "Worker exited before terminal state: " + str(child.pid)
                    )
                await asyncio.sleep(0.5)
            raise TimeoutError("Release qualification deadline; original run retained")

        async def request(name, *, rollback=False):
            try:
                version = (await deployment.show("primary"))["deployment"]["version"]
            except Exception:
                if name != "old" or rollback:
                    raise
                version = 0
            row = await deployment.request(
                "primary",
                None if rollback else refs[name],
                name + ("-rollback" if rollback else ""),
                version,
                "qualification",
                {
                    "task_kind": "inline-static-review",
                    "payload": {"sample": source, "goal": "检查空列表并引用检索证据"},
                },
                timeout=300,
                rollback=rollback,
            )
            return row["probe_run_id"]

        async def retain(name, run):
            attempt = run["attempts"][-1]
            items = (await gate.list(run["id"]))["items"]
            async with db.engine.connect() as connection:
                blobs = (
                    (
                        await connection.execute(
                            select(artifacts).where(artifacts.c.run_id == run["id"])
                        )
                    )
                    .mappings()
                    .all()
                )
            report["runs"][name] = {
                "run_id": run["id"],
                "attempt_id": run["attempt_id"],
                "state": run["state"],
                "revision_ref": run["input"]["revision_ref"],
                "work_authorization_ref": run["input"]["work_authorization_ref"],
                "session_id": attempt["session_id"],
                "process_id": attempt["process_id"],
                "worker_id": attempt["worker_id"],
                "tool_states": [x["state"] for x in items],
                "approval_states": [x["approval_state"] for x in items],
                "artifacts": [
                    {
                        "id": a["id"],
                        "digest": a["digest"],
                        "storage_key": a["storage_key"],
                    }
                    for a in blobs
                ],
            }
            save(root / "progress.json", report)
            print(
                json.dumps({"stage": name, "state": run["state"], "run_id": run["id"]}),
                flush=True,
            )

        first = await request("old")
        run = await wait(first, start("old"))
        assert run["state"] == "succeeded", run["state"]
        await retain("initial-health", run)
        active = await deployment.reconcile("primary")
        assert active["status"]["active_revision_id"] == refs["old"]
        business = await console.submit(
            profile_id, "first-item", "验证版本绑定与发布排空", "old-business"
        )
        old_worker = start("old")
        running = await wait(business["id"], old_worker, stop_at_approval=True)
        assert (
            running["state"] not in {"succeeded", "failed", "cancelled"}
            and old_worker.poll() is None
        )
        original_attempt = running["attempt_id"]
        second = await request("new")
        run = await wait(second, start("new"))
        assert run["state"] == "succeeded"
        await retain("candidate-health", run)
        draining = await deployment.reconcile("primary")
        assert (
            draining["deployment"]["state"] == "draining"
            and draining["unsettled_runs"] == 1
        )
        assert old_worker.poll() is None
        run = await wait(business["id"], old_worker)
        assert (
            run["state"] == "succeeded"
            and run["attempt_id"] == original_attempt
            and len(run["attempts"]) == 1
        )
        await retain("old-business", run)
        old_artifacts = report["runs"]["old-business"]["artifacts"]
        active = await deployment.reconcile("primary")
        assert active["status"]["active_revision_id"] == refs["new"]
        bad = await request("bad")
        run = await wait(bad, start("bad"))
        assert run["state"] == "failed"
        await retain("failed-candidate", run)
        active = await deployment.reconcile("primary")
        assert active["status"]["active_revision_id"] == refs["new"]
        restored = await request("old", rollback=True)
        run = await wait(restored, start("old"))
        assert run["state"] == "succeeded"
        await retain("rollback-health", run)
        assert (
            report["runs"]["rollback-health"]["work_authorization_ref"]
            != report["runs"]["initial-health"]["work_authorization_ref"]
        )
        for name in (
            "initial-health",
            "candidate-health",
            "old-business",
            "rollback-health",
        ):
            assert report["runs"][name]["tool_states"]
            assert all(s == "succeeded" for s in report["runs"][name]["tool_states"])
            assert all(s == "consumed" for s in report["runs"][name]["approval_states"])
        final = await deployment.reconcile("primary")
        assert final["status"]["active_revision_id"] == refs["old"]
        for item in old_artifacts:
            assert provider.verify(item["storage_key"]).digest == item["digest"]
        original = await console.detail(business["id"])
        assert (
            original["revision_ref"] == refs["old"]
            and original["deployment_id"] == "primary"
        )
        assert {(a["id"], a["digest"]) for a in original["artifacts"]} == {
            (a["id"], a["digest"]) for a in old_artifacts
        }
        report.update(
            passed=True,
            revisions=refs,
            images={name: s.image for name, s in specs.items()},
            worker_processes=[p.pid for p in children],
            final=await deployment.show("primary"),
            tool_executions=server.calls,
            console_run=business["id"],
            checks=[
                "published configuration survives original edits and controlled-source deletion",
                "real independent Worker process/session and approved tool evidence",
                "candidate waits for original active Attempt to finish naturally",
                "failed candidate retains known good version",
                "rollback uses fresh E2AG Work and approval",
                "original Run Attempt and artifact identities remain readable",
            ],
        )
        save(root / "result.json", report)
        print(
            json.dumps(
                {"passed": True, "scope": scope, "result": str(root / "result.json")}
            ),
            flush=True,
        )
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    await asyncio.to_thread(child.wait, 10)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    await asyncio.to_thread(child.wait, 10)
        for log in logs:
            log.close()
        server.close()
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(qualify(parser.parse_args()))
