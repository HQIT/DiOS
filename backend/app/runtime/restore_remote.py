"""Authenticated original-result reconciliation while all execution remains held."""

import asyncio
import time

from sqlalchemy import select

from app.services.e2ag import Decision

from .authorization import authorize_work
from .database import RuntimeErrorCode
from .lifecycle import require_restore_hold
from .remote_schema import peers, receipts, require, uploads
from .schema import attempts, runs
from .store import ACTIVE_ATTEMPTS, TERMINAL, RuntimeStore, digest, identifier
from .work_authorizations import WorkAuthorizationVerifier, audit


async def request(control, certificate, expiry, envelope, method, params):
    failure = None
    async with control.store.db.transaction(control.store.scope) as c:
        await require(c)
        now = await control.store.db.now(c)
        if expiry <= max(now, time.time()):
            raise RuntimeErrorCode("WORKER_CERTIFICATE_EXPIRED")
        machine = await asyncio.to_thread(
            control.identities.machine,
            certificate,
            envelope["scope"],
            envelope["profile"],
            envelope["worker_id"],
            now=now,
        )
        try:
            async with c.begin_nested():
                result = await handle(
                    control, c, machine, envelope, method, params, now
                )
        except RuntimeErrorCode as exc:
            failure = exc
            binding = params.get("binding")
            if (
                isinstance(binding, list)
                and len(binding) == 5
                and isinstance(binding[0], str)
            ):
                run = (
                    await c.execute(
                        select(runs.c.id).where(
                            runs.c.id == binding[0], runs.c.scope == control.store.scope
                        )
                    )
                ).scalar_one_or_none()
                if run:
                    await audit(
                        c,
                        control.store.scope,
                        run,
                        None,
                        "restore-result",
                        Decision(
                            "restore-reconciliation",
                            "deny",
                            (exc.code,),
                            evidence={
                                "certificate_sha256": certificate,
                                "method": method,
                            },
                        ),
                        now,
                    )
    if failure:
        raise failure
    return result


async def handle(control, c, machine, envelope, method, params, now):
    from .remote_control import TransactionView

    allowed = {
        "prepare": {"binding", "version"},
        "get": {"binding"},
        "authorize": {"binding", "action", "session_id"},
        "upload.begin": {"binding", "key", "size"},
        "upload.chunk": {"binding", "key", "offset", "data"},
        "upload.finish": {"binding", "key"},
        "resolve_exit": {
            "binding",
            "version",
            "receipt",
            "session_id",
            "result_key",
            "failure",
        },
    }
    if method not in allowed or set(params) != allowed[method]:
        raise RuntimeErrorCode("RESTORE_METHOD_DENIED")
    if method == "authorize" and params["action"] != "accept_result":
        raise RuntimeErrorCode("RESTORE_EXECUTION_DENIED")
    await require_restore_hold(c)
    scope = control.store.scope
    store = RuntimeStore(TransactionView(control.store.db, c, scope), scope)
    binding = params["binding"]
    if (
        not isinstance(binding, list)
        or len(binding) != 5
        or type(binding[4]) is not int
    ):
        raise RuntimeErrorCode("INVALID_BINDING")
    for value in binding[:4]:
        identifier(value)
    run = await store._run(c, binding[0])
    attempt = (
        (
            await c.execute(
                select(attempts).where(
                    attempts.c.id == binding[1], attempts.c.run_id == run["id"]
                )
            )
        )
        .mappings()
        .first()
    )
    if not attempt or (
        attempt["worker_id"],
        attempt["worker_instance"],
        attempt["epoch"],
    ) != tuple(binding[2:]):
        raise RuntimeErrorCode("STALE_OWNER")
    peer = (
        (
            await c.execute(
                select(peers).where(
                    peers.c.id == attempt["worker_id"], peers.c.scope == scope
                )
            )
        )
        .mappings()
        .first()
    )
    # Current allowlist authorizes the same durable machine identity after certificate rotation.
    # No peer row, ownership lease or execution grant is adopted by this channel.
    if (
        not peer
        or peer["machine_id"] != machine["worker_id"]
        or peer["profile"] != envelope["profile"]
    ):
        raise RuntimeErrorCode("WORKER_IDENTITY_SCOPE_DENIED")
    if (
        run["attempt_id"] != attempt["id"]
        or run["input"].get("runtime_profile") != envelope["profile"]
        or run["input"].get("profile_digest") != envelope["profile_digest"]
        or run["input"].get("revision_ref") != envelope.get("revision_ref")
    ):
        raise RuntimeErrorCode("STALE_OWNER")
    rid = digest(
        [
            "restore",
            scope,
            machine["worker_id"],
            envelope["profile"],
            envelope["request_id"],
        ]
    )
    hashed = digest(
        [envelope["message"], envelope["profile_digest"], envelope.get("revision_ref")]
    )
    old = (
        (await c.execute(select(receipts).where(receipts.c.id == rid)))
        .mappings()
        .first()
    )
    if old:
        if old["digest"] != hashed:
            raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
        # Retries only read the previously committed receipt; never reapply a result.
        return old["response"]
    if method == "upload.begin" and run["state"] in TERMINAL:
        row = (
            (
                await c.execute(
                    select(uploads).where(
                        uploads.c.scope == scope,
                        uploads.c.attempt_id == attempt["id"],
                        uploads.c.key == params["key"],
                        uploads.c.complete == True,
                    )
                )
            )
            .mappings()
            .first()
        )
        if not row or type(params["size"]) is not int or row["size"] != params["size"]:
            raise RuntimeErrorCode("ARTIFACT_ACCESS_DENIED")
        return {"received": row["size"], "complete": True}
    if method == "prepare":
        if type(params["version"]) is not int or run["version"] != params["version"]:
            raise RuntimeErrorCode("VERSION_CONFLICT")
        if attempt["state"] in ACTIVE_ATTEMPTS:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(state="result_unknown", error={"code": "RESTORED_OWNER_FENCED"})
            )
            await store._event(
                c,
                run,
                "attempt.restore_fenced",
                {"attempt_id": attempt["id"]},
                now,
                state="reconciliation_required",
                epoch=run["epoch"] + 1,
            )
            run = await store._run(c, run["id"])
            attempt = dict(attempt, state="result_unknown")
    if (
        run["state"] != "reconciliation_required"
        or attempt["state"] != "result_unknown"
        or run["epoch"] != attempt["epoch"] + 1
    ):
        raise RuntimeErrorCode("RESTORE_RECONCILIATION_STATE_REQUIRED")
    await authorize_work(
        store,
        run,
        attempt,
        "accept_result",
        session_id=params.get("session_id", attempt["session_id"]),
        verifier=WorkAuthorizationVerifier(store, control.policy, restore=True),
    )
    if method in {"prepare", "get"}:
        result = await store.get(run["id"])
        result["attempts"] = [a for a in result["attempts"] if a["id"] == attempt["id"]]
    elif method == "authorize":
        result = True
    elif method.startswith("upload."):
        result = await control.upload(c, store, attempt, params, method, now)
    else:
        receipt = params["receipt"]
        if not isinstance(receipt, dict) or not {"stdout", "stderr"} <= receipt.keys():
            raise RuntimeErrorCode("INVALID_EXIT_RECEIPT")
        for key in (receipt["stdout"], receipt["stderr"], params["result_key"]):
            if key is not None:
                await control.blob(c, store, attempt, key)
        await store.resolve_exit(
            dict(run, version=params["version"]),
            attempt,
            receipt,
            control.provider,
            session_id=params["session_id"],
            result_key=params["result_key"],
            failure=params["failure"],
            restore=True,
        )
        result = await store.get(run["id"])
        await audit(
            c,
            scope,
            run["id"],
            attempt["id"],
            "restore-result",
            Decision(
                "worker-identity",
                "allow",
                ("RESTORE_RESULT_RECONCILED",),
                evidence={
                    "certificate_sha256": machine["certificate_sha256"],
                    "machine_id": machine["worker_id"],
                    "original_epoch": attempt["epoch"],
                    "receipt_digest": digest(receipt),
                },
            ),
            now,
        )
    if method in {"prepare", "resolve_exit"}:
        await c.execute(
            receipts.insert().values(
                id=rid, scope=scope, digest=hashed, response=result, created_at=now
            )
        )
    return result
