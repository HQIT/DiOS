"""Operator-admitted remote versions; possession of a digest grants no access."""

import asyncio

from .database import RuntimeErrorCode
from .execution_revisions import load_profile


async def resolve(db, scope, entry, profile, profile_digest, revision_ref=None):
    from .work_policy import security_profile
    from .worker import read_profile

    if not isinstance(entry, dict) or entry.get("enabled") is not True:
        raise RuntimeErrorCode("WORKER_PROFILE_DENIED")
    if revision_ref is not None:
        allowed = entry.get("revisions", [])
        if (
            not isinstance(allowed, list)
            or len(allowed) > 64
            or not isinstance(revision_ref, str)
            or revision_ref not in allowed
        ):
            raise RuntimeErrorCode("WORKER_REVISION_DENIED")
        _, selected, spec, mode, refs = await load_profile(db, scope, revision_ref)
    else:
        if "path" not in entry:
            raise RuntimeErrorCode("WORKER_REVISION_REQUIRED")
        selected, spec = await asyncio.to_thread(read_profile, entry["path"])
        mode, refs = await asyncio.to_thread(security_profile, entry["path"])
    if mode != "e2ag" or selected != profile or spec.fingerprint() != profile_digest:
        raise RuntimeErrorCode("PROFILE_CHANGED")
    return spec, refs
