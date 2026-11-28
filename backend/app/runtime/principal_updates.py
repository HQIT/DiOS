"""Trusted Linux administration of versioned identity catalog entries."""

import asyncio
import copy
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from sqlalchemy import select, and_
from .database import RuntimeErrorCode
from .principals import FilePrincipals
from .schema import runs
from .work_auth_schema import work, grants
from .delegation_schema import links
from .delegation_identity_schema import issuers
from .delegations import revoke_record


def projection(catalog, principal, scope):
    """Trusted administration view for external policy planners; no credentials."""
    document = FilePrincipals(catalog).document()
    row = document["principals"].get(principal)
    if row is None or row["scope"] != scope:
        raise RuntimeErrorCode("AUTH_ACTION_DENIED")
    return {"issuer": document["issuer"], "scope": scope, "principal_id": principal,
            "revision": row["revision"], "enabled": row["enabled"], "profiles": row["profiles"]}


def matching(binding, issuer, principal, revision):
    return and_(
        binding["issuer"].as_string() == issuer,
        binding["id"].as_string() == principal,
        binding["revision"].as_integer() <= revision,
    )


def stage(path, document):
    raw = (json.dumps(document, ensure_ascii=False) + "\n").encode()
    if len(raw) > 262144:
        raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")
    fd, name = tempfile.mkstemp(prefix=".identity-update-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        FilePrincipals(name).document()
        return Path(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def publish(staged, target):
    os.replace(staged, target)
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def update(store, catalog, principal, expected_revision, changes):
    if (
        type(expected_revision) is not int
        or expected_revision < 1
        or not isinstance(changes, dict)
        or not changes
        or set(changes) - {"enabled", "expires_at", "tokens", "profiles", "objects"}
    ):
        raise RuntimeErrorCode("INVALID_PRINCIPAL_UPDATE")
    path = Path(catalog)
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")
    info = path.stat()
    parent = path.parent.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o022
    ):
        raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")
    # All managed writers use one stable sidecar inode, including after rename.
    lock = os.open(
        str(path) + ".update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    staged = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeErrorCode("IDENTITY_UPDATE_BUSY") from None
        document = FilePrincipals(path).document()
        old = document["principals"].get(principal)
        if old is None or old["scope"] != store.scope:
            raise RuntimeErrorCode("AUTH_ACTION_DENIED")
        replay = old["revision"] == expected_revision + 1 and all(
            old.get(k) == v for k, v in changes.items()
        )
        if old["revision"] != expected_revision and not replay:
            raise RuntimeErrorCode("VERSION_CONFLICT")
        replacement = copy.deepcopy(document)
        replacement["principals"][principal].update(
            changes, revision=expected_revision + 1
        )
        staged = stage(path, replacement)
        async with store.db.transaction(store.scope) as c:
            # Scope serialization shares the dispatch/revoke ordering boundary.
            if FilePrincipals(path).document() != document:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            direct_runs = (
                (
                    await c.execute(
                        select(runs.c.id, runs.c.attempt_id)
                        .join(work, work.c.run_id == runs.c.id)
                        .where(
                            work.c.scope == store.scope,
                            runs.c.scope == store.scope,
                            work.c.state == "active",
                            matching(
                                runs.c.input["principal"],
                                document["issuer"],
                                principal,
                                expected_revision,
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            affected_links = (
                (
                    await c.execute(
                        select(links)
                        .join(
                            issuers,
                            and_(
                                issuers.c.delegation_ref == links.c.id,
                                issuers.c.scope == links.c.scope,
                            ),
                        )
                        .where(
                            links.c.scope == store.scope,
                            links.c.state == "active",
                            matching(
                                issuers.c.principal,
                                document["issuer"],
                                principal,
                                expected_revision,
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for link in affected_links:
                await revoke_record(
                    c,
                    store.db,
                    store.scope,
                    link,
                    reason="AUTH_PRINCIPAL_REVISION_CHANGED",
                )
            affected_work = (
                select(work.c.id)
                .join(runs, runs.c.id == work.c.run_id)
                .where(
                    work.c.scope == store.scope,
                    runs.c.scope == store.scope,
                    matching(
                        runs.c.input["principal"],
                        document["issuer"],
                        principal,
                        expected_revision,
                    ),
                )
            )
            revoked = await c.execute(
                work.update()
                .where(work.c.id.in_(affected_work), work.c.state == "active")
                .values(state="revoked", version=work.c.version + 1)
            )
            await c.execute(
                grants.update()
                .where(
                    grants.c.authorization_id.in_(affected_work),
                    grants.c.state == "active",
                )
                .values(state="revoked", version=grants.c.version + 1)
            )
            from .work_authorizations import audit
            from app.services.e2ag import Decision

            now = await store.db.now(c)
            for run in direct_runs:
                await audit(
                    c,
                    store.scope,
                    run["id"],
                    run["attempt_id"],
                    "identity.update",
                    Decision(
                        "principal-update", "deny", ("AUTH_PRINCIPAL_REVISION_CHANGED",)
                    ),
                    now,
                )
            # Publish the higher revision before commit. If commit fails, old
            # bindings still fail current-source validation; retry reconciles DB.
            if not replay:
                task = asyncio.create_task(asyncio.to_thread(publish, staged, path))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
        return {
            "principal_id": principal,
            "revision": expected_revision + 1,
            "already_published": replay,
            "revoked_delegations": len(affected_links),
            "direct_work_revoked": revoked.rowcount,
            "enabled": replacement["principals"][principal]["enabled"],
        }
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        os.close(lock)
