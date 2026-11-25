"""A2A 0.3 requests over the existing durable Runtime and E2AG authority."""

import asyncio
from datetime import UTC, datetime
from functools import wraps

from a2a.server.request_handlers import RequestHandler
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    Artifact,
    DataPart,
    FilePart,
    FileWithUri,
    HTTPAuthSecurityScheme,
    InternalError,
    InvalidParamsError,
    Part,
    PushNotificationNotSupportedError,
    SecurityScheme,
    Task,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskStatus,
    UnsupportedOperationError,
)
from a2a.utils.errors import ServerError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.runtime.database import RuntimeErrorCode
from app.runtime.store import TERMINAL, digest, identifier

PROTOCOL = "0.3.0"
DOCUMENTATION = "https://github.com/HQIT/DiOS/blob/feat/lightweight-runtime-32/docs/design/a2a-runtime-56-58.md"


class WorkInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: str = Field(min_length=1, max_length=2000)
    sample_id: str | None = Field(default=None, min_length=1, max_length=160)
    input_payload: dict | None = None

    @model_validator(mode="after")
    def exactly_one_input(self):
        if not self.goal.strip() or (self.sample_id is None) == (
            self.input_payload is None
        ):
            raise ValueError(
                "Supply goal and exactly one registered sample or input payload"
            )
        return self


def guarded(method):
    @wraps(method)
    async def call(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except ServerError:
            raise
        except RuntimeErrorCode as exc:
            if exc.code == "NOT_FOUND" or exc.code.startswith("AUTH_"):
                error = TaskNotFoundError()
            elif exc.code.startswith("INVALID_") or exc.code in {
                "IDEMPOTENCY_CONFLICT",
                "METADATA_TOO_LARGE",
                "SAMPLE_NOT_FOUND",
                "PROFILE_NOT_AVAILABLE",
            }:
                error = InvalidParamsError(message=exc.code)
            else:
                error = InternalError(message="Runtime request unavailable")
            raise ServerError(error=error) from None
        except Exception:  # noqa: BLE001 - protocol boundary must redact dependency exceptions
            # SDK's generic exception response includes str(exc); keep paths,
            # database details and credential-provider errors out of the wire.
            raise ServerError(error=InternalError()) from None

    return call


def agent_card(profile, url):
    return AgentCard(
        name=profile["name"],
        description=profile["description"] or profile["name"],
        url=url,
        version=profile["profile_digest"],
        protocol_version=PROTOCOL,
        provider=AgentProvider(organization="DiOS", url="https://github.com/HQIT/DiOS"),
        documentation_url=DOCUMENTATION,
        preferred_transport="JSONRPC",
        capabilities=AgentCapabilities(
            streaming=False, push_notifications=False, state_transition_history=False
        ),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        security_schemes={
            "bearer": SecurityScheme(root=HTTPAuthSecurityScheme(scheme="bearer"))
        },
        security=[{"bearer": []}],
        skills=[
            AgentSkill(
                id=profile["id"],
                name=profile["name"],
                tags=["dios-runtime"],
                description="Submit one DataPart containing goal and exactly one of sample_id or "
                "input_payload. Uses registered application input validation. "
                "Set configuration.blocking=false; poll tasks/get for completion.",
            )
        ],
    )


class RuntimeA2A(RequestHandler):
    """Per-request authenticated view; no SDK TaskStore or model execution here."""

    def __init__(self, console, profile_id, runtime_url):
        self.console, self.profile_id = console, profile_id
        self.runtime_url = runtime_url.rstrip("/")

    async def bound_run(self, task_id, action="run.read"):
        identifier(task_id)
        run = await self.console.authorize(task_id, "run.read")
        if action != "run.read":
            run = await self.console.authorize(task_id, action)
        binding = run["input"].get("transport_binding", {})
        if (
            binding.get("protocol"),
            binding.get("version"),
            run["input"].get("runtime_profile"),
        ) != ("a2a", PROTOCOL, self.profile_id):
            raise RuntimeErrorCode("NOT_FOUND")
        return run

    async def project(self, task_id):
        run = await self.bound_run(task_id)
        view = await self.console.detail(task_id)
        state = {
            "queued": "submitted",
            "executing": "working",
            "validating": "working",
            "retry_wait": "working",
            "reconciliation_required": "unknown",
            "succeeded": "completed",
            "failed": "failed",
            "cancelled": "canceled",
        }.get(view["state"], "unknown")
        artifacts = []
        if view["state"] == "succeeded" and "artifact.read" in view["access"]:
            for blob in view["artifacts"]:
                if blob["kind"] != "result":
                    continue
                # Check live object permission, presence, size and digest before
                # advertising a result. Downloads authenticate again separately.
                await self.console.artifact(task_id, blob["id"])
                artifacts.append(
                    Artifact(
                        artifact_id=blob["id"],
                        name=blob["id"] + ".json",
                        parts=[
                            Part(
                                root=FilePart(
                                    file=FileWithUri(
                                        uri=f"{self.runtime_url}/runs/{task_id}/artifacts/{blob['id']}/download",
                                        mime_type="application/json",
                                    )
                                )
                            )
                        ],
                        metadata={
                            "dios": {
                                "sha256": blob["digest"],
                                "size": blob["size"],
                                "attemptId": blob["attempt_id"],
                            }
                        },
                    )
                )
        await self.console.authorize(task_id, "run.read")
        return Task(
            id=task_id,
            context_id=run["input"]["transport_binding"]["context_id"],
            status=TaskStatus(
                state=state,
                timestamp=datetime.fromtimestamp(view["updated_at"], UTC).isoformat(),
            ),
            artifacts=artifacts or None,
            metadata={
                "dios": {
                    "runId": task_id,
                    "version": view["version"],
                    "runtimeState": view["state"],
                    "cancelRequested": view["cancel_requested"],
                    "observedAt": view["observed_at"],
                    "attemptId": view["attempt_id"],
                }
            },
        )

    @guarded
    async def on_message_send(self, params, context=None):
        msg, config = params.message, params.configuration
        identifier(msg.message_id)
        if msg.context_id is not None:
            identifier(msg.context_id)
        if msg.task_id is not None or msg.extensions or msg.reference_task_ids:
            raise ServerError(error=UnsupportedOperationError())
        if config:
            if config.blocking is True:
                raise ServerError(
                    error=UnsupportedOperationError(
                        message="Use blocking=false and tasks/get"
                    )
                )
            if config.push_notification_config is not None:
                raise ServerError(error=PushNotificationNotSupportedError())
            if (
                config.accepted_output_modes
                and "application/json" not in config.accepted_output_modes
            ):
                from a2a.types import ContentTypeNotSupportedError

                raise ServerError(error=ContentTypeNotSupportedError())
            if config.history_length is not None and config.history_length < 0:
                raise ServerError(error=InvalidParamsError())
        if (
            msg.role != "user"
            or len(msg.parts) != 1
            or not isinstance(msg.parts[0].root, DataPart)
        ):
            raise ServerError(
                error=InvalidParamsError(
                    message="Supply one user DataPart with the registered work input"
                )
            )
        try:
            value = WorkInput.model_validate(msg.parts[0].root.data)
        except ValidationError:
            raise ServerError(
                error=InvalidParamsError(message="Invalid registered work input")
            ) from None
        principal = self.console.principal
        owner = (
            {k: principal[k] for k in ("issuer", "id", "scope", "subject", "actor")}
            if principal
            else None
        )
        key = "a2a-" + digest([PROTOCOL, self.profile_id, owner, msg.message_id])
        binding = {
            "protocol": "a2a",
            "version": PROTOCOL,
            "message_id": msg.message_id,
            "context_id": msg.context_id or "ctx-" + digest([self.console.scope, key]),
            "request_digest": digest(
                {
                    "message": msg.model_dump(mode="json", exclude_none=True),
                    "metadata": params.metadata,
                }
            ),
        }
        run = await self.console.submit(
            self.profile_id,
            value.sample_id,
            value.goal,
            key,
            input_payload=value.input_payload,
            transport_binding=binding,
        )
        return await self.project(run["id"])

    @guarded
    async def on_get_task(self, params, context=None):
        if params.history_length is not None and params.history_length < 0:
            raise ServerError(error=InvalidParamsError())
        return await self.project(params.id)

    @guarded
    async def on_cancel_task(self, params, context=None):
        # Version conflicts re-observe the same task; they never create work.
        for _ in range(3):
            run = await self.bound_run(params.id, "run.cancel")
            if run["state"] == "cancelled":
                return await self.project(params.id)
            if run["state"] in TERMINAL:
                raise ServerError(error=TaskNotCancelableError())
            if run["cancel_requested"]:
                return await self.project(params.id)
            try:
                await self.console.store.cancel(
                    params.id,
                    run["version"],
                    access_guard=lambda r: self.console.check_access(r, "run.cancel"),
                )
                return await self.project(params.id)
            except RuntimeErrorCode as exc:
                if exc.code != "VERSION_CONFLICT":
                    raise
                await asyncio.sleep(0)
        raise ServerError(
            error=InternalError(message="Task changed; query tasks/get before retrying")
        )

    async def on_message_send_stream(self, params, context=None):
        raise ServerError(error=UnsupportedOperationError())
        yield

    async def on_resubscribe_to_task(self, params, context=None):
        raise ServerError(error=UnsupportedOperationError())
        yield

    async def on_set_task_push_notification_config(self, params, context=None):
        raise ServerError(error=PushNotificationNotSupportedError())

    on_get_task_push_notification_config = on_set_task_push_notification_config
    on_list_task_push_notification_config = on_set_task_push_notification_config
    on_delete_task_push_notification_config = on_set_task_push_notification_config
