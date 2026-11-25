"""Explicit recovery client: uploads retained results, never starts or stops work."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from .artifacts import LocalArtifacts
from .bridge import JsonlBridge
from .database import RuntimeErrorCode
from .remote_runtime import RemoteRuntimeStore
from .remote_tool_channel import RemoteToolProvider
from .store import encoded
from .worker import read_profile


class RestoreRemoteClient(RemoteRuntimeStore):
    def __init__(self, scope, transport):
        super().__init__(scope, transport)
        self.instance = "restore-" + uuid4().hex

    async def call(self, method, params, request_id=None):
        if method not in {
            "prepare",
            "get",
            "authorize",
            "upload.begin",
            "upload.chunk",
            "upload.finish",
            "resolve_exit",
        }:
            raise RuntimeErrorCode("RESTORE_METHOD_DENIED")
        return await super().call("restore." + method, params, request_id)

    async def prepare(self, binding, version):
        value = await self.bound("prepare", binding, version=version)
        self.bindings[binding[0]] = list(binding)
        return value


async def reconcile_receipt(client, provider, receipt, version):
    binding = [
        receipt[k]
        for k in ("run_id", "attempt_id", "worker_id", "worker_instance", "epoch")
    ]
    if receipt.get("scope") != client.scope:
        raise RuntimeErrorCode("WORKER_IDENTITY_SCOPE_DENIED")
    observation = receipt.get("observation", {})
    if observation.get("alive") is not False or observation.get("state") not in {
        "exited",
        "dead",
    }:
        raise RuntimeErrorCode("PROCESS_EXIT_NOT_CONFIRMED")
    if (
        receipt.get("truncated")
        or receipt.get("failure")
        or observation.get("exit_code") != 0
    ):
        raise RuntimeErrorCode("RESTORE_RESULT_REQUIRES_REVIEW")
    stdout = await asyncio.to_thread(provider.verify, receipt["stdout"])
    if stdout.size > 1024 * 1024:
        raise RuntimeErrorCode("OUTPUT_LIMIT_EXCEEDED")
    bridge = JsonlBridge()
    bridge.feed(
        await asyncio.to_thread(lambda: b"".join(provider.read(stdout.key))), final=True
    )
    run = await client.prepare(binding, version)
    from .result_contract import validate_result

    validate_result(run["input"], bridge.result)
    result = await asyncio.to_thread(provider.put, [encoded(bridge.result)])
    attempt = next(a for a in run["attempts"] if a["id"] == binding[1])
    return await client.resolve_exit(
        run,
        attempt,
        receipt,
        provider,
        session_id=bridge.session_id,
        result_key=result.key,
        failure=None,
    )


async def cli(args):
    profile, spec = await asyncio.to_thread(read_profile, args.profile_file)
    transport = RemoteToolProvider(
        args.control_file, SimpleNamespace(scope=args.scope), profile, spec
    )
    if args.revision_ref is not None:
        transport.revision_ref = args.revision_ref
    path = Path(args.receipt_file)
    if path.is_symlink() or path.stat().st_size > 16384:
        raise RuntimeErrorCode("INVALID_EXIT_RECEIPT")
    receipt = json.loads(path.read_text())
    return await reconcile_receipt(
        RestoreRemoteClient(args.scope, transport),
        LocalArtifacts(args.artifacts),
        receipt,
        args.expected_version,
    )
