"""Run one original reference input through a fixed real Harness and E2AG.

The independent knowledge Provider filters before retrieval. An isolated SQLite
scope owns all mutations; source credentials and existing deployments are read-only.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from uuid import uuid4
import sys

import qualify_harness_governance as base

ROOT = base.ROOT
sys.path[:0] = [str(ROOT / "applications/knowledge-source/src")]
from dios_knowledge_source.provider import FileKnowledgeProvider, handler
from app.runtime.execution_revisions import compile_profile
from app.runtime.revisions import Revisions
from app.runtime.application_contracts import INTERFACES


async def qualify(args):
    folder = args.output.resolve()
    folder.mkdir(parents=True, mode=0o700, exist_ok=False)
    scope = "reference-harness-" + uuid4().hex[:12]
    profile = json.loads(args.profile.read_text())
    assert profile["launch"]["image"].startswith("sha256:")
    catalog = json.loads(
        (
            ROOT / "examples/reference-workflows" / (args.scenario + "-inputs.json")
        ).read_text()
    )
    sample = catalog["inputs"][0]
    profile["input_mount_targets"] = []
    profile["application_contract"] = {
        "schema_version": 1,
        "id": "reference-workflows",
        "version": "0.1.1",
        "deployment": "independent",
        "requires": {
            **{key: ">=1,<2" for key in INTERFACES},
            "worker-jsonl": ">=1,<2",
            "governed-tool-channel": ">=1,<2",
        },
        "input": {
            "kind": "external.reference-workflow/v1",
            "schema_digest": "sha256:" + base.digest(catalog["schema"]),
            "result_schema_digest": "sha256:" + base.digest(catalog["result_schema"]),
        },
    }
    profile["console"] = dict(
        enabled=True,
        name=sample["name"],
        implementation=args.harness,
        synthetic=True,
        input_kind="external.reference-workflow/v1",
        input_catalog_data=catalog,
    )
    knowledge_ref = {
        "ref": "qualification/knowledge",
        "version": "v1",
        "target": "/run/secrets/knowledge",
    }
    profile["secrets"].append(knowledge_ref)
    profile["tool_channel"] = {
        "tools": {"knowledge_search": "reference-source"},
        "acp_names": {"mcp__dios__knowledge_search": "knowledge_search"},
        "worker_secret_refs": [
            r["ref"] for r in profile["secrets"] if r != knowledge_ref
        ],
    }
    base.save(folder / "profile.json", profile)
    pid, spec = base.read_profile(folder / "profile.json", allow_unfrozen_packages=True)
    token = uuid4().hex
    secret = folder / "knowledge-credential"
    secret.write_text(token)
    secret.chmod(0o600)
    base.save(
        folder / "knowledge-grants.json",
        {
            "version": 1,
            "grants": [
                {
                    "enabled": True,
                    "subject": "reference-" + args.scenario,
                    "project_id": sample["context_id"],
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "documents": sample["source_refs"],
                }
            ],
        },
    )
    knowledge = FileKnowledgeProvider(
        ROOT / "examples/reference-workflows/catalog.json",
        folder / "knowledge-grants.json",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(knowledge))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    db = base.Database("sqlite+aiosqlite:///" + str(folder / "runtime.sqlite"))
    provider = base.LocalArtifacts(folder / "objects")
    adapter = base.DockerRuntimeAdapter(scope)
    worker_task = None
    report = {
        "harness": args.harness,
        "scenario": args.scenario,
        "scope": scope,
        "image": spec.image,
        "input_digest": sample["sha256"],
        "passed": False,
    }
    try:
        await db.migrate()
        store = base.RuntimeStore(db, scope)
        revision = (
            await Revisions(db, scope).create(
                "reference-v1", compile_profile(profile), actor="operator:qualification"
            )
        )["revision"]["id"]
        registration = {
            "enabled": True,
            "transport": "mcp-http",
            "url": f"http://127.0.0.1:{server.server_port}/mcp",
            "name": "search",
            "approval_required": True,
            "credential_ref": knowledge_ref,
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
            "action": "knowledge.search",
            "resource_kind": "mcp_tool",
            "resource_id": "reference-source",
            "resource_version": base.digest(
                {k: v for k, v in registration.items() if k != "enabled"}
            ),
            "constraint_revision": "reference-harness",
        }
        registration["permission"] = permission
        base.save(
            folder / "tools.json",
            {"version": 1, "tools": {"reference-source": registration}},
        )
        permissions = [
            dict(
                action="runtime." + action,
                resource_kind="runtime_profile",
                resource_id=pid,
                resource_version=spec.fingerprint(),
                constraint_revision="reference-harness",
            )
            for action in ("execute", "accept_result")
        ] + [permission]
        delegation = await base.Delegations(store).issue(
            "reference-work",
            subject="operator:qualification",
            actor="service:reference",
            permissions=permissions,
            expires_at=base.time.time() + 1800,
            policy_evaluator=json.loads(args.opa.read_text()),
        )
        base.save(
            folder / "policy.json",
            {
                "version": 1,
                "policy_id": "reference-harness",
                "subject": "operator:qualification",
                "actor": "service:reference",
                "profiles": {
                    pid: {
                        "profile_digest": spec.fingerprint(),
                        "secret_refs": profile["secrets"],
                        "work_ttl_seconds": 1200,
                        "delegation_ref": delegation["id"],
                        "permissions": {
                            layer: permissions
                            for layer in ("caller", "service", "workload", "resource")
                        },
                    }
                },
            },
        )
        registry = json.loads(args.secrets.read_text())["secrets"]
        selected = {
            ref["ref"]: dict(registry[ref["ref"]], scope=scope, profiles=[pid])
            for ref in profile["secrets"]
            if ref != knowledge_ref
        }
        selected[knowledge_ref["ref"]] = dict(
            state="active",
            scope=scope,
            profiles=[pid],
            version="v1",
            path=str(secret),
            sha256=hashlib.sha256(token.encode()).hexdigest(),
        )
        base.save(folder / "secrets.json", {"version": 1, "secrets": selected})
        approval_token = uuid4().hex
        base.save(
            folder / "approvers.json",
            {
                "version": 1,
                "approvers": [
                    {
                        "enabled": True,
                        "subject": "operator:qualification-reviewer",
                        "token_sha256": hashlib.sha256(
                            approval_token.encode()
                        ).hexdigest(),
                        "scopes": [scope],
                        "tools": ["reference-source"],
                    }
                ],
            },
        )
        policy = base.FileWorkPolicy(folder / "policy.json")
        gate = base.ToolInvocations(
            store,
            policy,
            base.FileToolCatalog(folder / "tools.json"),
            provider,
            base.McpHttpTransport(),
            base.FileApprovers(folder / "approvers.json"),
            secrets=base.FileSecretMounts(folder / "secrets.json", scope),
        )
        worker = base.Worker(
            store,
            adapter,
            pid,
            spec,
            provider,
            timeout=240,
            poll_interval=0.1,
            authorization_verifier=base.WorkAuthorizationVerifier(store, policy),
            secret_resolver=base.FileSecretMounts(folder / "secrets.json", scope),
            secret_requests=profile["secrets"],
            tool_gate=gate,
            revision_ref=revision,
        )
        value = {
            "runtime_profile": pid,
            "profile_digest": spec.fingerprint(),
            "secret_refs": profile["secrets"],
            "task_kind": "external.reference-workflow/v1",
            "payload": {
                "sample": sample,
                "goal": "依据授权来源完成参考任务，保留待人工复核状态",
            },
        }
        run = await store.submit(
            "reference-" + args.scenario,
            value,
            authorization=policy.admission(pid, spec.fingerprint(), profile["secrets"]),
            revision_ref=revision,
        )
        report.update(run_id=run["id"], revision=revision)
        base.save(folder / "progress.json", report)
        worker_task = asyncio.create_task(worker.once())
        deadline = base.time.monotonic() + 260
        decisions = []
        while not worker_task.done() and base.time.monotonic() < deadline:
            for item in (await gate.list(run["id"]))["items"]:
                if item["state"] == "awaiting_approval":
                    assert item["arguments"] == {"query": sample["query"]}
                    decisions.append(
                        await gate.decide(
                            run["id"],
                            item["id"],
                            item["version"],
                            "approved",
                            approval_token,
                            "合成参考场景授权检索",
                        )
                    )
            await asyncio.sleep(0.1)
        if not worker_task.done():
            raise TimeoutError("Original Worker retained until owned cleanup")
        await worker_task
        final = await store.get(run["id"])
        base.save(folder / "run.json", final)
        assert final["state"] == "succeeded", final["attempts"][-1]["error"]
        assert len(final["attempts"]) == 1
        attempt = final["attempts"][0]
        receipt = base.ExitReceipts(provider, scope).load(attempt)
        assert receipt and not receipt["observation"]["alive"]
        frames = [
            json.loads(line)
            for line in b"".join(provider.read(receipt["stdout"])).splitlines()
        ]
        outputs = [row["result"] for row in frames if row.get("type") == "result"]
        assert len(outputs) == 1
        output = outputs[0]
        assert (
            output["input_digest"] == sample["sha256"]
            and output["review_status"] == "pending_review"
        )
        assert {row["id"]: row["digest"] for row in output["citations"]} == sample[
            "source_refs"
        ]
        tools = (await gate.list(run["id"]))["items"]
        assert (
            len(tools) == 1
            and tools[0]["state"] == "succeeded"
            and tools[0]["approval_state"] == "consumed"
        )
        invocation = await gate.get(run["id"], tools[0]["id"])
        assert (
            invocation["session_id"] == attempt["session_id"]
            and invocation["attempt_id"] == attempt["id"]
        )
        report.update(
            passed=True,
            attempt=attempt,
            output=output,
            invocation=invocation,
            authorization=await base.view(store, run["id"]),
            frames=frames,
            receipt=receipt,
            state=final["state"],
            approval_count=len(decisions),
            worker_pid=os.getpid(),
        )
        base.save(folder / "result.json", report)
        print(
            json.dumps(
                {k: report[k] for k in ("harness", "scenario", "run_id", "passed")}
            ),
            flush=True,
        )
    finally:
        if worker_task and not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        containers = await asyncio.to_thread(
            adapter.client.containers.list,
            all=True,
            filters={"label": "dios.runtime.scope=" + adapter.scope},
        )
        for container in containers:
            await asyncio.to_thread(container.remove, force=True)
        adapter.client.close()
        provider.close()
        await db.close()
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, required=True)
    parser.add_argument("--opa", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--harness", choices=("cursor", "dsh"), required=True)
    parser.add_argument(
        "--scenario", choices=("sales", "team", "education"), required=True
    )
    asyncio.run(qualify(parser.parse_args()))


if __name__ == "__main__":
    main()
