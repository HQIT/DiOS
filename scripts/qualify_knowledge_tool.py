"""Opt-in Linux qualification: real Runtime HTTP, official MCP and E2AG socket.

Uses isolated synthetic data. No Harness/model is launched; the existing Cursor
ACP qualification covers the unchanged upstream tool-channel integration.
"""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
from qualify_object_lifecycle import SERVER

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests'), str(ROOT / 'sdk/python/src')]
from dios_sdk import TaskClient
from app.runtime.adapter import LaunchSpec
from app.runtime.artifacts import LocalArtifacts
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.database import Database
from app.runtime.mcp_tool_transport import McpHttpTransport
from app.runtime.secret_provider import FileSecretMounts
from app.runtime.store import RuntimeStore, digest
from app.runtime.tool_catalog import FileApprovers, FileToolCatalog
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker_tool_channel import WorkerToolChannel
from work_auth_fixtures import policy_fixture


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def governed(root, endpoint, token, changed, principal_revoke):
    db = Database('sqlite+aiosqlite:///' + str(root / 'worker.sqlite'))
    await db.migrate()
    store = RuntimeStore(db, 'qualification')
    provider = LocalArtifacts(root / 'worker-objects')
    spec = LaunchSpec('controlled-fixture', ('no-model',))
    secret = {'ref': 'knowledge-reader', 'version': '1', 'target': '/run/secrets/knowledge'}
    policy, _ = policy_fixture(root, 'knowledge', spec, [secret])
    registration = dict(enabled=True, transport='mcp-http', url=endpoint, name='search',
        credential_ref=secret, approval_required=True,
        input_schema={'type': 'object', 'properties': {'query': {'type': 'string'}},
                      'required': ['query'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'content': {'type': 'array'}}, 'required': ['content']})
    permission = dict(action='knowledge.search', resource_kind='mcp_tool', resource_id='knowledge/search',
        resource_version=digest({k: v for k, v in registration.items() if k != 'enabled'}), constraint_revision='test-policy')
    registration['permission'] = permission
    document = json.loads(policy.path.read_text())
    for rows in document['profiles']['knowledge']['permissions'].values():
        rows.append(permission)
    write(policy.path, document)
    write(root / 'tools.json', {'version': 1, 'tools': {'search': registration}})
    approval = secrets.token_urlsafe(32)
    write(root / 'approvers.json', {'version': 1, 'approvers': [{
        'enabled': True, 'subject': 'operator:independent', 'token_sha256': hashlib.sha256(approval.encode()).hexdigest(),
        'scopes': ['qualification'], 'tools': ['search']}]})
    credential = root / 'mcp-token'
    credential.write_text(token)
    credential.chmod(0o600)
    write(root / 'secrets.json', {'version': 1, 'secrets': {'knowledge-reader': {
        'state': 'active', 'scope': 'qualification', 'profiles': ['knowledge'], 'version': '1',
        'path': str(credential), 'sha256': hashlib.sha256(credential.read_bytes()).hexdigest()}}})
    descriptor = policy.admission('knowledge', spec.fingerprint(), [secret])
    run = await store.submit('governed-knowledge', dict(runtime_profile='knowledge',
        profile_digest=spec.fingerprint(), secret_refs=[secret]), authorization=descriptor)
    await store.register_worker('worker', 'instance', 'local', os.getpid())
    attempt = await store.claim('worker', 'instance')
    binding = (run['id'], attempt['id'], 'worker', 'instance', attempt['epoch'])
    context = WorkAuthorizationContext('qualification', *binding[:2], 'worker', 'instance', attempt['epoch'],
        attempt['authorization_epoch'], None, run['input']['work_authorization_ref'])
    verifier = WorkAuthorizationVerifier(store, policy)
    await verifier(context, 'execute')
    await store.report(*binding, 1, 'preparing')
    await store.report(*binding, 2, 'creating_session')
    await verifier(replace(context, session_id='controlled-session'), 'execute')
    await store.report(*binding, 3, 'waiting_model', session_id='controlled-session')
    gate = ToolInvocations(store, policy, FileToolCatalog(root / 'tools.json'), provider,
        McpHttpTransport(), FileApprovers(root / 'approvers.json'), FileSecretMounts(root / 'secrets.json', 'qualification'))
    channel = WorkerToolChannel(gate, binding, {'tools': {'knowledge_search': 'search'}}, os.getuid())

    async def call(method, **params):
        reader, writer = await asyncio.open_unix_connection(str(channel.directory / 'channel.sock'), limit=2 * 1024 * 1024)
        try:
            writer.write(json.dumps({'method': method, 'params': params}).encode() + b'\n')
            await writer.drain()
            result = json.loads(await reader.readline())
            assert result['ok'], result
            return result['value']
        finally:
            writer.close()
            await writer.wait_closed()

    async def query(key):
        pending = await call('request', tool='knowledge_search', arguments={'query': '共同'}, key=key)
        waiting = await call('advance', invocation_id=pending['id'])
        assert waiting['state'] == 'awaiting_approval', waiting
        await gate.decide(run['id'], pending['id'], pending['version'], 'approved', approval)
        return await call('advance', invocation_id=pending['id'])

    try:
        await channel.open()
        await channel.bind_session('controlled-session')
        first = await query('first')
        assert first['state'] == 'succeeded', first
        result = json.loads(first['result']['content'][0]['text'])
        assert len(result['items']) == 1 and result['items'][0]['source_id'] == 'a', result
        await asyncio.to_thread(changed)
        after = await query('after-source-delete')
        assert after['state'] == 'succeeded' and json.loads(after['result']['content'][0]['text'])['items'] == []
        # Completed invocation replay is historical evidence, not a fresh query.
        replay = await call('advance', invocation_id=first['id'])
        assert replay['result'] == first['result']
        await asyncio.to_thread(principal_revoke)
        pending = await call('request', tool='knowledge_search', arguments={'query': '共同'}, key='revoked-reader')
        await gate.decide(run['id'], pending['id'], pending['version'], 'approved', approval)
        reader, writer = await asyncio.open_unix_connection(str(channel.directory / 'channel.sock'))
        writer.write(json.dumps({'method': 'advance', 'params': {'invocation_id': pending['id']}}).encode() + b'\n')
        await writer.drain()
        denied = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        assert not denied['ok'], denied
        current = await store.get(run['id'])
        await store.revoke(run['id'], current['version'])
        reader, writer = await asyncio.open_unix_connection(str(channel.directory / 'channel.sock'))
        writer.write(json.dumps({'method': 'advance', 'params': {'invocation_id': first['id']}}).encode() + b'\n')
        await writer.drain()
        revoked = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        assert not revoked['ok'], revoked
        return {'run_id': run['id'], 'attempt_id': attempt['id'], 'invocation_id': first['id'],
            'citation': result['items'][0], 'approval_required': True, 'delete_fresh_query_empty': True,
            'historical_receipt_retained': True, 'revoked_reader_error': denied['error'],
            'work_revocation_blocks_receipt_replay': revoked['error'], 'real_unix_peer_credentials': True}
    finally:
        await channel.close()
        provider.close()
        await db.close()
        credential.unlink(missing_ok=True)


def qualify(root, adapter_python):
    root = root.resolve()
    if not root.is_relative_to(ROOT / 'workspace') or root.exists():
        raise ValueError('Use a new isolated repository workspace directory')
    root.mkdir(mode=0o700)
    (root / 'profiles').mkdir()
    (root / 'object_server.py').write_text(SERVER)
    tokens = {key: secrets.token_urlsafe(32) for key in ('operator', 'a', 'b', 'mcp')}
    now = time.time()
    principals = {'version': 1, 'issuer': 'qualification:knowledge', 'audience': 'dios-runtime', 'principals': {}}
    for key in ('a', 'b'):
        principals['principals'][key] = dict(subject=key, actor=key, scope='qualification', enabled=True,
            revision=1, expires_at=now + 1800, profiles={}, objects={},
            tokens=[dict(sha256=hashlib.sha256(tokens[key].encode()).hexdigest(), enabled=True,
                         not_before=now - 1, expires_at=now + 1800)])
    write(root / 'principals.json', principals)
    write(root / 'policy.json', {'version': 1, 'identity_catalog': str(root / 'principals.json')})
    write(root / 'server.json', {'scope': 'qualification', 'token': tokens['operator']})
    async def initialize():
        db = Database('sqlite+aiosqlite:///' + str(root / 'state.sqlite'))
        try:
            await db.migrate()
        finally:
            await db.close()
    asyncio.run(initialize())
    base = 'http://127.0.0.1:' + str(port())
    endpoint = 'http://127.0.0.1:' + str(port()) + '/mcp'
    reader_file = root / 'reader-token'
    reader_file.write_text(tokens['a'])
    reader_file.chmod(0o600)
    binding = dict(token_sha256=hashlib.sha256(tokens['mcp'].encode()).hexdigest(), enabled=True, expires_at=now + 1800,
        runtime_token_file=str(reader_file), runtime_token_sha256=hashlib.sha256(reader_file.read_bytes()).hexdigest(),
        principal=dict(issuer=principals['issuer'], audience='dios-runtime', scope='qualification', id='a', subject='a', actor='a'))
    write(root / 'bindings.json', {'version': 1, 'runtime_url': base, 'bindings': [binding]})
    processes, logs = [], []
    def start(args, name, env=None):
        log = (root / (name + '.log')).open('wb')
        logs.append(log)
        processes.append(subprocess.Popen(args, cwd=ROOT / 'backend', env=env, stdout=log, stderr=subprocess.STDOUT))
    def ready(url, token, status):
        with httpx.Client(trust_env=False) as client:
            for _ in range(100):
                try:
                    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}
                    if url.endswith('/mcp'):
                        response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'id': 'ready',
                            'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                            'clientInfo': {'name': 'qualification', 'version': '1'}}})
                    else:
                        response = client.get(url, headers=headers)
                    if response.status_code == status:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
        raise RuntimeError('Qualification service failed readiness: ' + url + ' status=' + str(response.status_code))
    try:
        start([str(ROOT / 'backend/.venv/bin/python'), '-m', 'uvicorn', 'object_server:app', '--host', '127.0.0.1',
               '--port', base.rsplit(':', 1)[1]], 'runtime',
              dict(os.environ, QUALIFICATION_ROOT=str(root), PYTHONPATH=str(ROOT / 'backend') + ':' + str(root)))
        ready(base + '/api/os/runtime/knowledge/capabilities', tokens['operator'], 200)
        clients = {k: TaskClient(base, tokens[k]) for k in ('operator', 'a', 'b')}
        for key in ('a', 'b'):
            ref = clients['operator'].publish_file(('共同 来源 ' + key).encode(), idempotency_key=key)['ref']
            clients['operator'].set_object_policy('file', ref['id'], [{'principal_id': key,
                'actions': ['object.read', 'object.download'], 'expires_at': now + 1800}], expected_revision=0, idempotency_key='grant-' + key)
            clients['operator'].replace_knowledge_source(key, ref,
                {'namespace': 'fixture', 'key': key, 'version': 'v1', 'locator': 'record/' + key},
                expected_revision=0, idempotency_key='bind-' + key)
        start([str(adapter_python), '-m', 'dios_knowledge_mcp.server', '--config', str(root / 'bindings.json'),
               '--port', endpoint.split(':')[-1].split('/')[0]], 'mcp')
        ready(endpoint, tokens['mcp'], 200)
        with httpx.Client(trust_env=False) as http:
            assert http.get(endpoint).status_code == 403
            assert http.get(endpoint, headers={'Authorization': 'Bearer ' + tokens['a']}).status_code == 403
        def deleted():
            clients['operator'].delete_knowledge_source('a', expected_revision=1, idempotency_key='delete-a')
        def revoked():
            principals['principals']['a']['enabled'] = False
            write(root / 'principals.json', principals)
        result = asyncio.run(governed(root, endpoint, tokens['mcp'], deleted, revoked))
        result.update(passed=True, new_model_calls=0, fixture='controlled-session; real HTTP and Unix transport',
            protocol='2025-06-18', mcp_sdk='1.30.0', separate_credentials=True,
            provider=clients['operator'].knowledge_capabilities()['provider'])
        write(root / 'result.json', result)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
        for name in ('reader-token', 'bindings.json', 'server.json', 'principals.json'):
            (root / name).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--adapter-python', type=Path, required=True)
    args = parser.parse_args()
    qualify(args.directory, args.adapter_python.absolute())
