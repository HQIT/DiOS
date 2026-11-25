"""Operator access to external declaration evidence for an immutable Revision."""

import asyncio
from pathlib import Path

from .a2a_profile import A2AProfile
from .a2a_transport import A2ATransport
from .database import RuntimeErrorCode
from .endpoint_compatibility import Compatibility
from .execution_revisions import load_profile


class RevisionCompatibility:
    def __init__(self, db, scope, policy=None, secret_resolver=None):
        self.db, self.scope, self.policy, self.secret_resolver = (
            db,
            scope,
            policy,
            secret_resolver,
        )
        self.repository = Compatibility(db, scope)

    async def show(self, revision_ref):
        _, _, spec, _, _ = await load_profile(self.db, self.scope, revision_ref)
        external = isinstance(spec, A2AProfile)
        observation = None
        if external:
            try:
                observation = await self.repository.show(spec.fingerprint())
            except RuntimeErrorCode as exc:
                if exc.code != "NOT_FOUND":
                    raise
        return {
            "revision_ref": revision_ref,
            "profile_digest": spec.fingerprint(),
            "configuration_rollback": "supported",
            "implementation_visibility": "declared"
            if observation
            else "opaque"
            if external
            else "pinned-image",
            "implementation_rollback": "unsupported" if external else "pinned-image",
            "implementation_digest": None if external else spec.image,
            "model_implementation_visibility": "opaque",
            "model_implementation_rollback": "unsupported",
            "qualification": observation,
            "state": observation["state"]
            if observation
            else "unobserved"
            if external
            else "pinned",
        }

    async def observe(self, revision_ref, *, transport_factory=A2ATransport):
        _, profile, spec, mode, refs = await load_profile(
            self.db, self.scope, revision_ref
        )
        if not isinstance(spec, A2AProfile):
            raise RuntimeErrorCode("EXTERNAL_OBSERVATION_UNSUPPORTED")
        if self.policy is None or mode != "e2ag":
            raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
        await asyncio.to_thread(
            self.policy.admission, profile, spec.fingerprint(), refs
        )
        if len(refs) > 1:
            raise RuntimeErrorCode("A2A_SINGLE_CREDENTIAL_REQUIRED")
        token = None
        if refs:
            if self.secret_resolver is None:
                raise RuntimeErrorCode("SECRET_AUTHORIZATION_REQUIRED")
            mounts = await asyncio.to_thread(
                self.secret_resolver.resolve, profile, refs
            )
            token = (
                await asyncio.to_thread(
                    Path(mounts[0].source).read_text, encoding="utf-8"
                )
            ).strip()
        transport = transport_factory(spec, token=token)
        try:
            await self.repository.discover(transport, spec)
        finally:
            await transport.aclose()
        return await self.show(revision_ref)

    async def accept(self, revision_ref, version, snapshot_digest, actor, evidence):
        _, _, spec, _, _ = await load_profile(self.db, self.scope, revision_ref)
        if not isinstance(spec, A2AProfile):
            raise RuntimeErrorCode("EXTERNAL_OBSERVATION_UNSUPPORTED")
        await self.repository.accept(
            spec.fingerprint(), version, snapshot_digest, actor, evidence
        )
        return await self.show(revision_ref)
