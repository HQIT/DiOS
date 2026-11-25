"""Loopback qualification application: independent principals, real HTTP SDK calls."""

import asyncio
import hashlib
import json
import secrets
import socket
import time

from fastapi import FastAPI
import uvicorn

from app.api.os import runtime_tasks
from app.middleware.access_token import AccessTokenMiddleware
from dios_sdk.tasks import TaskClient, ApiError


class RecipientApplication:
    def __init__(
        self,
        directory,
        scope,
        config,
        parent_id,
        child_id,
        parent_permissions,
        child_permissions,
    ):
        self.parent_id, self.child_id = parent_id, child_id
        self.child_permissions = child_permissions
        self.tokens = {
            name: secrets.token_urlsafe(32) for name in ("issuer", "recipient")
        }
        self.recipient_actor = "service:independent-recipient"
        now = time.time()
        principals = {}
        for name in self.tokens:
            principals[name] = dict(
                subject=config["subject"],
                actor=config["actor"] if name == "issuer" else self.recipient_actor,
                scope=scope,
                enabled=True,
                revision=1,
                expires_at=now + 3600,
                tokens=[
                    dict(
                        sha256=hashlib.sha256(self.tokens[name].encode()).hexdigest(),
                        enabled=True,
                        not_before=now - 10,
                        expires_at=now + 3600,
                    )
                ],
                profiles={},
            )
        principals["issuer"]["profiles"] = {
            parent_id: dict(
                actions=["run.submit", "run.read", "artifact.read"],
                permissions=parent_permissions,
            ),
            child_id: dict(
                actions=["delegation.issue", "delegation.read", "delegation.revoke"],
                permissions=child_permissions,
            ),
        }
        principals["recipient"]["profiles"] = {
            child_id: dict(
                actions=["run.submit", "run.read", "artifact.read"],
                permissions=child_permissions,
            )
        }
        path = directory / "principals.json"
        path.write_text(
            json.dumps(
                dict(
                    version=1,
                    issuer="qualification:recipient",
                    audience="dios-runtime",
                    principals=principals,
                )
            )
        )
        path.chmod(0o600)
        config["identity_catalog"] = str(path)
        self.server = self.server_task = self.sock = None
        self.old_settings = None

    async def start(self, console):
        app = FastAPI()
        app.include_router(runtime_tasks.router, prefix="/api/os")
        app.add_middleware(AccessTokenMiddleware)
        app.state.runtime_console = console
        settings = runtime_tasks.settings
        self.old_settings = (settings.runtime_console_enabled, settings.access_token)
        settings.runtime_console_enabled = True
        settings.access_token = secrets.token_urlsafe(32)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.sock.setblocking(False)
        self.server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False)
        )
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        async with asyncio.timeout(10):
            while not self.server.started:
                if self.server_task.done():
                    await self.server_task
                await asyncio.sleep(0.05)
        url = "http://127.0.0.1:" + str(self.sock.getsockname()[1])
        self.issuer = TaskClient(url, self.tokens["issuer"])
        self.recipient = TaskClient(url, self.tokens["recipient"])

    async def submit_parent(self, root_ref):
        return await asyncio.to_thread(
            self.issuer.submit,
            self.parent_id,
            "coordinate",
            "静态检查空列表边界，返回有代码证据的成果。",
            idempotency_key="recipient-parent",
            delegation_ref=root_ref,
        )

    async def handoff(self, parent, root_ref, input_id):
        leaf = await asyncio.to_thread(
            self.issuer.issue_delegation,
            root_ref,
            self.child_id,
            self.recipient_actor,
            self.child_permissions,
            time.time() + 1200,
            idempotency_key="recipient-handoff",
            parent_run_id=parent["id"],
        )
        child = await asyncio.to_thread(
            self.recipient.submit,
            self.child_id,
            input_id,
            "静态检查空列表边界，返回有代码证据的成果。",
            idempotency_key="recipient-child",
            delegation_ref=leaf["id"],
            parent_run_id=parent["id"],
        )
        replay = await asyncio.to_thread(
            self.recipient.submit,
            self.child_id,
            input_id,
            "静态检查空列表边界，返回有代码证据的成果。",
            idempotency_key="recipient-child",
            delegation_ref=leaf["id"],
            parent_run_id=parent["id"],
        )
        assert replay["id"] == child["id"]
        try:
            await asyncio.to_thread(self.issuer.get, child["id"])
        except ApiError as exc:
            assert exc.status == 404
        else:
            raise AssertionError("Issuer gained unrestricted recipient task access")
        return child, leaf

    async def close(self):
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            await asyncio.wait_for(self.server_task, 15)
        if self.sock:
            self.sock.close()
        if self.old_settings:
            (
                runtime_tasks.settings.runtime_console_enabled,
                runtime_tasks.settings.access_token,
            ) = self.old_settings
