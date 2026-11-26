"""Independent application lifecycle across Cursor and an existing alternative Harness.

Opt-in isolated native execution; model inputs contain synthetic source only.
"""

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
from app.runtime.database import Database, RuntimeErrorCode
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
    resume = args.resume_initial
    root.mkdir(mode=0o700, parents=True, exist_ok=resume)
    previous = json.loads((root / "progress.json").read_text()) if resume else None
    scope = previous["scope"] if resume else "application-lifecycle-" + uuid4().hex[:16]
    assert scope.startswith("application-lifecycle-") and root.is_relative_to(
        ROOT / "workspace"
    )
    db = Database("sqlite+aiosqlite:///" + str(root / "runtime.sqlite"))
    source = sample("first-item")
    server = SourceServer(source["source"], False)
    children = []
    logs = []
    refs = {}
    specs = {}
    report = previous or {"scope": scope, "runs": {}, "checks": []}
    try:
        await db.migrate()
        store = RuntimeStore(db, scope)
        provider = LocalArtifacts(root / "objects")
        base = json.loads(args.profile.read_text())
        profile_id = base["id"]
        assert base["governance"]["mode"] == "e2ag" and base["launch"][
            "image"
        ].startswith("sha256:")
        alternative = json.loads(args.alternative_profile.read_text())
        alternative["id"] = profile_id
        assert alternative["tool_channel"] == base["tool_channel"]
        contract_schema = {
            "type": "object",
            "required": [
                "summary",
                "findings",
                "markdown",
                "evidence",
                "review_status",
            ],
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "findings": {"type": "array"},
                "markdown": {"type": "string", "minLength": 1},
                "evidence": {"type": "array", "minItems": 1},
                "review_status": {"const": "pending_review"},
            },
        }
        catalog = {
            "version": 1,
            "inputs": [source],
            "input_digest": "source-sha256",
            "requires_e2ag": True,
            "schema": {
                "type": "object",
                "required": ["id", "name", "filename", "source", "sha256"],
            },
            "result_schema": contract_schema,
        }
        from app.runtime.application_contracts import INTERFACES

        contract = {
            "schema_version": 1,
            "id": "static-review",
            "version": "0.1.4",
            "deployment": "independent",
            "requires": {
                **{k: ">=1,<2" for k in INTERFACES},
                "worker-jsonl": ">=1,<2",
                "governed-tool-channel": ">=1,<2",
            },
            "input": {
                "kind": "external.static-review/v1",
                "schema_digest": "sha256:" + digest(catalog["schema"]),
                "result_schema_digest": "sha256:" + digest(contract_schema),
            },
        }
        if not resume:
            raw_profiles = {}
            for name in ("old", "new", "alternative", "bad"):
                raw = copy.deepcopy(
                    alternative if name in {"alternative", "bad"} else base
                )
                # Mount only explicitly granted model credentials, per gateway policy.
                execution_refs = [item["ref"] for item in raw.get("secrets", [])]
                assert all(ref.startswith("model/") for ref in execution_refs)
                raw["tool_channel"]["worker_secret_refs"] = execution_refs
                if name == "new":
                    raw["launch"]["image"] = args.upgrade_image
                raw["launch"].setdefault("environment", {})["DIOS_RELEASE_LABEL"] = name
                for index, mount in enumerate(raw["launch"].get("mounts", [])):
                    assert mount["read_only"] and Path(mount["source"]).is_file()
                    target = root / (
                        name
                        + "-controlled-"
                        + str(index)
                        + Path(mount["source"]).suffix
                    )
                    shutil.copyfile(mount["source"], target)
                    mount["source"] = str(target)
                raw["console"] = {
                    "enabled": True,
                    "name": "Independent application qualification",
                    "implementation": name,
                    "synthetic": True,
                    "input_kind": contract["input"]["kind"],
                    "input_catalog_data": catalog,
                }
                raw["application_contract"] = dict(
                    contract, version="0.1.4" if name == "old" else "0.1.5"
                )
                if name == "bad":
                    raw["launch"]["command"] = ["python", "-c", "raise SystemExit(7)"]
                path = root / (name + ".json")
                save(path, raw)
                raw_profiles[name] = raw
                row = await publish_profile(db, scope, path, name, "qualification")
                refs[name] = row["revision"]["id"]
                _, _, specs[name], _, _ = await load_profile(db, scope, refs[name])
                for mount in raw["launch"].get("mounts", []):
                    Path(mount["source"]).unlink()
            other = json.loads(args.other_profile.read_text())
            other["id"] = "other-independent-app"
            other["application_contract"]["id"] = "other-independent-app"
            other["launch"]["command"] = [
                "python",
                "-c",
                'import json,os,time; json.loads(os.environ["DIOS_WORK_INPUT"]); print(json.dumps({"type":"session","session_id":"other-independent-session"}),flush=True); time.sleep(20); print(json.dumps({"type":"result","session_id":"other-independent-session","result":{"summary":"other application completed","evidence":["synthetic"],"session_id":"other-independent-session","pid":1}}),flush=True)',
            ]
            save(root / "other.json", other)
            raw_profiles["other"] = other
            row = await publish_profile(
                db, scope, root / "other.json", "other", "qualification"
            )
            refs["other"] = row["revision"]["id"]
            _, _, specs["other"], _, _ = await load_profile(db, scope, refs["other"])
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
                        "resource_id": raw_profiles[name]["id"],
                        "resource_version": spec.fingerprint(),
                        "constraint_revision": "revision-release",
                    }
                    for action in ("execute", "accept_result")
                ] + ([] if name == "other" else [permission])
                rows[spec.fingerprint()] = {
                    "profile_digest": spec.fingerprint(),
                    "secret_refs": raw_profiles[name].get("secrets", []),
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
                    profile_id: {
                        **rows[specs["new"].fingerprint()],
                        "revisions": {
                            k: v
                            for k, v in rows.items()
                            if k != specs["other"].fingerprint()
                        },
                    },
                    "other-independent-app": rows[specs["other"].fingerprint()],
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
                for raw in raw_profiles.values()
                for ref in raw.get("secrets", [])
            }
            save(root / "secrets.json", {"version": 1, "secrets": selected})
        else:
            import threading
            from http.server import ThreadingHTTPServer
            from urllib.parse import urlparse

            from app.runtime.revision_schema import receipts
            from app.runtime.schema import runs

            assert set(report["runs"]) == {"initial-health"}
            async with db.read(scope) as connection:
                existing = (
                    (
                        await connection.execute(
                            select(runs).where(runs.c.scope == scope)
                        )
                    )
                    .mappings()
                    .all()
                )
                assert all(
                    r["state"] in {"succeeded", "failed", "cancelled"} for r in existing
                )
                refs = {
                    row.key: row.revision_id
                    for row in (
                        await connection.execute(
                            select(receipts).where(receipts.c.scope == scope)
                        )
                    )
                }
            assert set(refs) == {"old", "new", "alternative", "bad", "other"}
            for name, ref in refs.items():
                _, _, specs[name], _, _ = await load_profile(db, scope, ref)
            assert (await store.get(report["runs"]["initial-health"]["run_id"]))[
                "state"
            ] == "succeeded"
            report["retained_failed_trials"] = [
                {"run_id": r["id"], "state": r["state"]}
                for r in existing
                if r["state"] == "failed"
            ]
            policy = FileWorkPolicy(root / "policy.json")
            tool_id = base["tool_channel"]["tools"]["repository_search"]
            registration = json.loads((root / "tools.json").read_text())["tools"][
                tool_id
            ]
            # Reopen the exact fixture endpoint; fixed policy/version stay intact.
            handler = server.server.RequestHandlerClass
            server.close()
            server.server = ThreadingHTTPServer(
                ("127.0.0.1", urlparse(registration["url"]).port), handler
            )
            server.calls = sum(len(r["tool_states"]) for r in report["runs"].values())
            threading.Thread(target=server.server.serve_forever, daemon=True).start()
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
        profiles.mkdir(exist_ok=resume)
        save(
            profiles / "release.json",
            {"id": profile_id, "deployment_id": "primary", "enabled": True},
        )
        save(
            profiles / "other.json",
            {
                "id": "other-independent-app",
                "revision_ref": refs["other"],
                "enabled": True,
            },
        )
        console = TaskConsole(db, scope, profiles, provider.root, policy=policy)
        console.tools = gate

        def start(name, crash=False):
            log = (root / ("worker-" + uuid4().hex[:8] + ".log")).open("x")
            logs.append(log)
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                *(["--crash-after-output"] if crash else []),
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
                "600",
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
            deadline = asyncio.get_running_loop().time() + 660
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
                if child.poll() == 75:
                    await recover_native(
                        store, provider, policy, scope, run_id, refs, specs, report
                    )
                    return await store.get(run_id)
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
                    "task_kind": contract["input"]["kind"],
                    "input_registration": {
                        "digest": digest(catalog),
                        "result_schema": contract_schema,
                    },
                    "payload": {"sample": source, "goal": "检查空列表并引用检索证据"},
                },
                timeout=660,
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
                "application": run["input"]["application"],
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

        if not resume:
            first = await request("old")
            run = await wait(first, start("old", crash=True))
            assert run["state"] == "succeeded", run["state"]
            await retain("initial-health", run)
        active = await deployment.reconcile("primary")
        assert active["status"]["active_revision_id"] == refs["old"]
        business = await console.submit(
            profile_id,
            "first-item",
            "验证版本绑定与发布排空",
            "old-business-" + uuid4().hex,
        )
        old_worker = start("old")
        running = await wait(business["id"], old_worker, stop_at_approval=True)
        assert (
            running["state"] not in {"succeeded", "failed", "cancelled"}
            and old_worker.poll() is None
        )
        original_attempt = running["attempt_id"]
        save(
            profiles / "release.json",
            {"id": profile_id, "deployment_id": "primary", "enabled": False},
        )
        try:
            await console.submit(
                profile_id, "first-item", "must not enqueue", "disabled-task"
            )
        except RuntimeErrorCode as exc:
            assert exc.code == "PROFILE_NOT_AVAILABLE"
        else:
            raise AssertionError("Disabled application accepted work")
        other_run = await console.submit(
            "other-independent-app",
            "health",
            "unaffected application",
            "other-task",
        )
        other_worker = start("other")
        report["disabled_admission"] = "PROFILE_NOT_AVAILABLE"

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
        save(
            profiles / "release.json",
            {"id": profile_id, "deployment_id": "primary", "enabled": True},
        )
        replacement = await request("alternative")
        replacement_run = await wait(replacement, start("alternative", crash=True))
        assert replacement_run["state"] == "succeeded"
        await retain("alternative-health", replacement_run)
        switched = await deployment.reconcile("primary")
        assert switched["status"]["active_revision_id"] == refs["alternative"]
        bad = await request("bad")
        run = await wait(bad, start("bad"))
        assert run["state"] == "failed"
        await retain("failed-candidate", run)
        active = await deployment.reconcile("primary")
        assert active["status"]["active_revision_id"] == refs["alternative"]
        restored = await request("new", rollback=True)
        run = await wait(restored, start("new"))
        assert run["state"] == "succeeded"
        await retain("rollback-health", run)
        unaffected = await wait(other_run["id"], other_worker)
        assert unaffected["state"] == "succeeded"
        await retain("other-application", unaffected)
        assert (
            report["runs"]["rollback-health"]["work_authorization_ref"]
            != report["runs"]["initial-health"]["work_authorization_ref"]
        )
        for name in (
            "initial-health",
            "candidate-health",
            "alternative-health",
            "old-business",
            "rollback-health",
        ):
            assert report["runs"][name]["tool_states"]
            assert all(s == "succeeded" for s in report["runs"][name]["tool_states"])
            assert all(s == "consumed" for s in report["runs"][name]["approval_states"])
        final = await deployment.reconcile("primary")
        assert final["status"]["active_revision_id"] == refs["new"]
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
                "application 0.1.4 upgrades to independently built 0.1.5",
                "same application 0.1.5 runs through Cursor and DeepSeek Harness",
                "disabled application rejects new work; existing work finishes naturally",
                "other independent application completes during upgrade",
                "both implementations recover original complete output without model restart",
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
                    await asyncio.to_thread(child.wait, 30)
                except subprocess.TimeoutExpired:
                    print(
                        json.dumps({"retained_worker_pid": child.pid, "scope": scope}),
                        flush=True,
                    )
        for log in logs:
            log.close()
        server.close()
        await db.close()


async def recover_native(store, provider, policy, scope, run_id, refs, specs, report):
    from app.runtime.docker_adapter import DockerRuntimeAdapter
    from app.runtime.work_authorizations import WorkAuthorizationVerifier
    from app.runtime.worker import Worker

    run = await store.get(run_id)
    attempt = run["attempts"][0]
    reference = run["input"]["revision_ref"]
    name = next(k for k, v in refs.items() if v == reference)
    adapter = DockerRuntimeAdapter(scope)
    launches = 0

    async def forbidden(*_a, **_kw):
        nonlocal launches
        launches += 1
        raise AssertionError("Recovery launched another model")

    adapter.start = forbidden
    worker = Worker(
        store,
        adapter,
        run["input"]["runtime_profile"],
        specs[name],
        provider,
        revision_ref=reference,
        authorization_verifier=WorkAuthorizationVerifier(store, policy),
    )
    deadline = asyncio.get_running_loop().time() + 90
    while asyncio.get_running_loop().time() < deadline:
        await store.expire()
        current = await store.get(run_id)
        if current["state"] == "reconciliation_required":
            break
        await asyncio.sleep(0.5)
    else:
        raise TimeoutError("Original lease still active; no replacement started")
    observe = adapter.observe

    async def unavailable(_handle):
        raise TimeoutError("Injected observation timeout")

    adapter.observe = unavailable
    first = await worker.recover()
    assert first[0]["error"] == "OBSERVATION_UNAVAILABLE"
    adapter.observe = observe
    result = await worker.recover()
    assert any(x.get("state") == "succeeded" for x in result), result
    final = await store.get(run_id)
    assert final["attempt_id"] == attempt["id"] and len(final["attempts"]) == 1
    assert final["attempts"][0]["session_id"] == attempt["session_id"]
    assert (
        final["input"]["application"] == run["input"]["application"] and launches == 0
    )
    try:
        await store.heartbeat(
            run_id,
            attempt["id"],
            attempt["worker_id"],
            attempt["worker_instance"],
            attempt["epoch"],
        )
    except RuntimeErrorCode as exc:
        assert exc.code == "STALE_OWNER"
    else:
        raise AssertionError("Old ownership accepted")
    report.setdefault("recoveries", []).append(
        {
            "run_id": run_id,
            "attempt_id": attempt["id"],
            "session_id": attempt["session_id"],
            "revision_ref": reference,
            "model_restarts": launches,
            "observation_timeout_preserved": True,
            "old_owner_rejected": True,
        }
    )


def worker_main(argv):
    from app.runtime.cli import main
    from app.runtime.reconciliation import ExitReceipts

    if "--crash-after-output" in argv:
        argv.remove("--crash-after-output")

        async def crash(self, run_id, *_a, **_kw):
            assert self.scope.startswith("application-lifecycle-")
            root = Path(self.db.engine.url.database).parent.resolve()
            assert root.is_relative_to(ROOT / "workspace")
            run = await self.get(run_id)
            provider = LocalArtifacts(root / "objects")
            receipt = ExitReceipts(provider, self.scope).load(run["attempts"][0])
            assert receipt and not receipt["observation"]["alive"]
            save(
                root / ("crash-" + run_id + ".json"),
                {"worker_pid": os.getpid(), "run": run, "receipt": receipt},
            )
            os._exit(75)

        RuntimeStore.complete = crash
    return main(argv)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        raise SystemExit(worker_main(sys.argv[2:]))
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ["profile", "alternative-profile", "other-profile", "secrets", "output"]:
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--upgrade-image", required=True)
    parser.add_argument(
        "--resume-initial",
        action="store_true",
        help="Reuse successful initial health; all existing work must be terminal",
    )
    asyncio.run(qualify(parser.parse_args()))
