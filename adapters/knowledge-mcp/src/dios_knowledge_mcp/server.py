"""Official MCP transport with separately bound upstream read credentials."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import secrets
import time

from dios_sdk import TaskClient, WorkloadError
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp import types
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.requests import Request

INPUT_SCHEMA = {'type': 'object', 'properties': {
    'query': {'type': 'string', 'minLength': 1, 'maxLength': 500},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 3}},
    'required': ['query'], 'additionalProperties': False}
IDENTITY_FIELDS = {'issuer', 'audience', 'scope', 'id', 'subject', 'actor'}


async def bounded_thread(call):
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The SDK bounds each HTTP call to 15s. Do not orphan credentialed reads.
        try:
            await task
        except Exception:
            pass
        raise


def read_file(path, limit):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise WorkloadError('KNOWLEDGE_CREDENTIAL_INVALID')
    with path.open('rb') as source:
        raw = source.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise WorkloadError('KNOWLEDGE_CREDENTIAL_INVALID')
    return raw


class Bindings:
    def __init__(self, path):
        self.path = Path(path)

    def resolve(self, token):
        if not token:
            raise WorkloadError('KNOWLEDGE_ACCESS_DENIED')
        document = json.loads(read_file(self.path, 131072))
        if document.get('version') != 1 or not isinstance(document.get('bindings'), list):
            raise WorkloadError('KNOWLEDGE_CONFIGURATION_INVALID')
        hashed = hashlib.sha256(token.encode()).hexdigest()
        matches = [row for row in document['bindings']
                   if secrets.compare_digest(str(row.get('token_sha256', '')), hashed)]
        if len(matches) != 1:
            raise WorkloadError('KNOWLEDGE_ACCESS_DENIED')
        row = matches[0]
        if row.get('enabled') is not True or type(row.get('expires_at')) not in (int, float) or not time.time() < row['expires_at'] < float('inf'):
            raise WorkloadError('KNOWLEDGE_ACCESS_DENIED')
        expected = row.get('principal')
        if not isinstance(expected, dict) or set(expected) != IDENTITY_FIELDS:
            raise WorkloadError('KNOWLEDGE_CONFIGURATION_INVALID')
        raw = read_file(row['runtime_token_file'], 16384)
        if hashlib.sha256(raw).hexdigest() != row.get('runtime_token_sha256'):
            raise WorkloadError('KNOWLEDGE_CREDENTIAL_CHANGED')
        upstream = raw.decode('utf-8').strip()
        if secrets.compare_digest(token, upstream):
            raise WorkloadError('KNOWLEDGE_SEPARATE_CREDENTIAL_REQUIRED')
        client = TaskClient(document['runtime_url'], upstream, timeout=15, max_bytes=1024 * 1024)
        identity = client.identity()
        if identity.get('mode') != 'principal' or {k: identity['principal'].get(k) for k in IDENTITY_FIELDS} != expected:
            raise WorkloadError('KNOWLEDGE_IDENTITY_CHANGED')
        # The returned binding includes credential/config hashes for live recheck.
        return client, hashlib.sha256(json.dumps([document['runtime_url'], row], sort_keys=True).encode()).hexdigest()


def bearer(request):
    header = request.headers.get('authorization', '')
    return header[7:].strip() if header.lower().startswith('bearer ') else ''


def create_app(config):
    bindings = Bindings(config)
    server = Server('dios-knowledge', version='0.1.0')
    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True,
        max_request_body_size=65536,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=['127.0.0.1:*', 'localhost:*'],
            allowed_origins=['http://127.0.0.1:*', 'http://localhost:*']))

    @server.list_tools()
    async def tools():
        return [types.Tool(name='search', description='Read currently authorized DiOS source versions',
                           inputSchema=INPUT_SCHEMA, annotations=types.ToolAnnotations(readOnlyHint=True))]

    @server.call_tool()
    async def call(name, arguments):
        try:
            if name != 'search':
                raise WorkloadError('KNOWLEDGE_TOOL_UNAVAILABLE')
            request = server.request_context.request
            token = bearer(request)
            client, before = await bounded_thread(lambda: bindings.resolve(token))
            if before != request.scope['knowledge_binding']:
                raise WorkloadError('KNOWLEDGE_IDENTITY_CHANGED')
            found = await bounded_thread(lambda: client.search_knowledge(arguments['query'], limit=arguments.get('limit', 3)))
            _, after = await bounded_thread(lambda: bindings.resolve(token))
            if before != after:
                raise WorkloadError('KNOWLEDGE_IDENTITY_CHANGED')
            text = json.dumps(found, ensure_ascii=False, separators=(',', ':'))
            result = types.CallToolResult(content=[types.TextContent(type='text', text=text)], isError=False)
            # Leave space for the JSON-RPC envelope used by DiOS's 1 MiB transport.
            if len(result.model_dump_json().encode()) > 900 * 1024:
                raise WorkloadError('KNOWLEDGE_TOOL_RESULT_TOO_LARGE')
            return result
        except WorkloadError as exc:
            return types.CallToolResult(content=[types.TextContent(type='text', text=exc.code)], isError=True)
        except (OSError, ValueError, KeyError, TypeError):
            return types.CallToolResult(content=[types.TextContent(type='text', text='KNOWLEDGE_UPSTREAM_UNAVAILABLE')], isError=True)

    class Endpoint:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            try:
                _, binding = await bounded_thread(lambda: bindings.resolve(bearer(request)))
                scope['knowledge_binding'] = binding
            except WorkloadError:
                await JSONResponse({'detail': 'KNOWLEDGE_ACCESS_DENIED'}, status_code=403,
                                   headers={'Cache-Control': 'no-store'})(scope, receive, send)
                return
            except (OSError, ValueError, KeyError, TypeError):
                await JSONResponse({'detail': 'KNOWLEDGE_UPSTREAM_UNAVAILABLE'}, status_code=503,
                                   headers={'Cache-Control': 'no-store'})(scope, receive, send)
                return
            async def no_store(message):
                if message['type'] == 'http.response.start':
                    message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() != b'cache-control'] + [(b'cache-control', b'no-store')]
                await send(message)
            await manager.handle_request(scope, receive, no_store)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield
    return Starlette(routes=[Route('/mcp', Endpoint(), methods=['GET', 'POST', 'DELETE'])], lifespan=lifespan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=13187)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.config.resolve()), host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
