"""Official A2A client with bounded I/O and pinned credential destinations."""

import base64
import hashlib
import json
from urllib.parse import unquote, urlsplit

import httpx
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import (
    AgentCard,
    DataPart,
    FilePart,
    FileWithBytes,
    MessageSendParams,
    Task,
    TaskIdParams,
    TaskQueryParams,
)

from .database import RuntimeErrorCode
from .store import encoded


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, spec, transport=None):
        self.spec = spec
        self.inner = transport or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        url = str(request.url)
        permitted = (
            (request.method == "POST" and url == self.spec.endpoint)
            or (request.method == "GET" and url == self.spec.card_url)
            or (request.method == "GET" and self.artifact_url(url))
        )
        if not permitted:
            raise RuntimeErrorCode("A2A_DESTINATION_DENIED")
        response = await self.inner.handle_async_request(request)
        try:
            if response.headers.get("content-encoding", "identity") != "identity":
                raise RuntimeErrorCode("A2A_ENCODING_UNSUPPORTED")
            data = bytearray()
            async for chunk in response.aiter_raw():
                data.extend(chunk)
                if len(data) > self.spec.response_limit:
                    raise RuntimeErrorCode("A2A_RESPONSE_TOO_LARGE")
            if request.method == "POST" and response.is_success:
                try:
                    sent = json.loads(await request.aread())
                    received = json.loads(data)
                except (ValueError, UnicodeError, RecursionError):
                    raise RuntimeErrorCode("A2A_RESPONSE_INVALID") from None
                if (
                    not isinstance(received, dict)
                    or received.get("jsonrpc") != "2.0"
                    or received.get("id") != sent.get("id")
                    or type(received.get("id")) is not type(sent.get("id"))
                ):
                    raise RuntimeErrorCode("A2A_RESPONSE_BINDING_CHANGED")
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(data),
                request=request,
            )
        finally:
            await response.aclose()

    def artifact_url(self, value):
        url = urlsplit(value)
        return (
            value.startswith(self.spec.artifact_prefix)
            and not url.query
            and not url.fragment
            and not any(p in {".", ".."} for p in unquote(url.path).split("/"))
            and "\\" not in unquote(url.path)
            and "%" not in unquote(url.path)
        )

    async def aclose(self):
        await self.inner.aclose()


class A2ATransport:
    def __init__(self, spec, *, token=None, transport=None):
        spec.validate()
        if token is not None and (not token or any(c.isspace() for c in token)):
            raise RuntimeErrorCode("A2A_CREDENTIAL_INVALID")
        self.spec = spec
        self.pinned = PinnedTransport(spec, transport)
        self.http = httpx.AsyncClient(
            transport=self.pinned,
            timeout=spec.request_timeout,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept-Encoding": "identity",
                **({"Authorization": "Bearer " + token} if token else {}),
            },
        )
        # Passing a minimal card disables the SDK's automatic extended-card fetch.
        self.client = JsonRpcTransport(
            self.http,
            agent_card=AgentCard(
                name="Pinned remote",
                description="Registered remote execution",
                version="1",
                protocol_version="0.3.0",
                url=spec.endpoint,
                capabilities={},
                skills=[],
                default_input_modes=["application/json"],
                default_output_modes=["application/json"],
            ),
            url=spec.endpoint,
        )

    async def discover(self):
        response = await self.http.get(self.spec.card_url)
        response.raise_for_status()
        card = AgentCard.model_validate(response.json())
        if (
            card.protocol_version != "0.3.0"
            or card.url != self.spec.endpoint
            or card.preferred_transport != "JSONRPC"
        ):
            raise RuntimeErrorCode("A2A_PROFILE_MISMATCH")
        if not any(s.id == self.spec.skill_id for s in card.skills):
            raise RuntimeErrorCode("A2A_SKILL_NOT_FOUND")
        if any(e.required for e in card.capabilities.extensions or []):
            raise RuntimeErrorCode("A2A_EXTENSION_UNSUPPORTED")
        return card

    async def submit(self, message):
        value = await self.client.send_message(
            MessageSendParams.model_validate(
                {
                    "message": message,
                    "configuration": {"blocking": False, "historyLength": 0},
                }
            )
        )
        if not isinstance(value, Task):
            raise RuntimeErrorCode("A2A_DURABLE_TASK_REQUIRED")
        return value

    async def observe(self, task_id):
        return await self.client.get_task(TaskQueryParams(id=task_id, history_length=0))

    async def cancel(self, task_id):
        return await self.client.cancel_task(TaskIdParams(id=task_id))

    async def result(self, task):
        if task.status.state != "completed" or len(task.artifacts or []) != 1:
            raise RuntimeErrorCode("A2A_RESULT_UNSUPPORTED")
        artifact = task.artifacts[0]
        if len(artifact.parts) != 1:
            raise RuntimeErrorCode("A2A_RESULT_UNSUPPORTED")
        part = artifact.parts[0].root
        if isinstance(part, DataPart):
            data = encoded(part.data)
        elif isinstance(part, FilePart):
            if part.file.mime_type != "application/json":
                raise RuntimeErrorCode("A2A_RESULT_UNSUPPORTED")
            if isinstance(part.file, FileWithBytes):
                try:
                    data = base64.b64decode(part.file.bytes, validate=True)
                except ValueError:
                    raise RuntimeErrorCode("A2A_RESULT_INVALID") from None
            else:
                if not self.pinned.artifact_url(part.file.uri):
                    raise RuntimeErrorCode("A2A_DESTINATION_DENIED")
                response = await self.http.get(part.file.uri)
                response.raise_for_status()
                data = response.content
        else:
            raise RuntimeErrorCode("A2A_RESULT_UNSUPPORTED")
        if len(data) > 65536:
            raise RuntimeErrorCode("A2A_RESULT_TOO_LARGE")
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise RuntimeErrorCode("A2A_RESULT_INVALID") from None
        advertised = (artifact.metadata or {}).get("dios", {})
        if (
            "sha256" in advertised
            and hashlib.sha256(data).hexdigest() != advertised["sha256"]
        ):
            raise RuntimeErrorCode("ARTIFACT_DIGEST_MISMATCH")
        if "size" in advertised and len(data) != advertised["size"]:
            raise RuntimeErrorCode("ARTIFACT_DIGEST_MISMATCH")
        return data

    async def aclose(self):
        await self.http.aclose()
