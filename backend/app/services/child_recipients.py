"""Authenticate a recipient through normal admission and retain the live parent owner."""

import asyncio
import json

from sqlalchemy import select

from app.runtime.database import RuntimeErrorCode
from app.runtime.principals import admit
from app.runtime.schema import attempts
from app.runtime.work_policy import policy_matches
from app.runtime.worker import read_profile
from app.services.child_tasks import configuration


async def prepare(console, parent_id, profile_id, profile_digest, reference):
    if console.principal is None:
        raise RuntimeErrorCode("AUTH_PRINCIPAL_REQUIRED")
    if reference is None:
        raise RuntimeErrorCode("AUTH_DELEGATION_REQUIRED")
    async with console.db.read(console.scope) as c:
        parent = await console.store._run(c, parent_id)
        attempt = (
            (
                await c.execute(
                    select(attempts).where(
                        attempts.c.id == parent["attempt_id"],
                        attempts.c.run_id == parent_id,
                    )
                )
            )
            .mappings()
            .first()
        )
    if attempt is None or not attempt["session_id"]:
        raise RuntimeErrorCode("CHILD_PARENT_NOT_RUNNING")

    def registered():
        current = admit(
            console.policy,
            parent["input"]["runtime_profile"],
            parent["input"]["profile_digest"],
            parent["input"].get("secret_refs", []),
            parent["input"].get("principal"),
        )
        if not policy_matches(current, parent["input"]["work_policy_digest"]):
            raise RuntimeErrorCode("WORK_POLICY_CHANGED")
        matches = []
        for path in console.profiles_dir.glob("*.json"):
            if path.stat().st_size > 65536:
                raise RuntimeErrorCode("INVALID_PROFILE_CATALOG")
            raw = json.loads(path.read_text())
            if raw.get("id") == parent["input"]["runtime_profile"]:
                _, spec = read_profile(path, value=raw)
                if spec.fingerprint() != parent["input"]["profile_digest"]:
                    raise RuntimeErrorCode("PROFILE_CHANGED")
                config = configuration(raw.get("tool_channel", {}).get("children"))
                granted = [
                    entry
                    for entry in config["profiles"].values()
                    if entry["profile_id"] == profile_id
                    and entry["profile_digest"] == profile_digest
                    and entry.get("authenticated_recipient") is True
                ]
                if not granted:
                    raise RuntimeErrorCode("CHILD_RECIPIENT_NOT_GRANTED")
                matches.append(config)
        if len(matches) != 1:
            raise RuntimeErrorCode("CHILD_RECIPIENT_NOT_GRANTED")
        return matches[0]

    config = await asyncio.to_thread(registered)
    binding = {
        "owner": [
            parent_id,
            attempt["id"],
            attempt["worker_id"],
            attempt["worker_instance"],
            attempt["epoch"],
        ],
        "session_id": attempt["session_id"],
        "max_children": config["max_children"],
        "max_depth": config["max_depth"],
    }

    async def guard(c):
        # The profile can change while identity/OPA requests wait.
        if await asyncio.to_thread(registered) != config:
            raise RuntimeErrorCode("PROFILE_CHANGED")

    return binding, guard
