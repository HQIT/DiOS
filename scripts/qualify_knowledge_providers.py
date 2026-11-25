"""Real HTTP adapter switch using one isolated installation and two principals."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time

import docker
import httpx
from qualify_object_lifecycle import SERVER

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'sdk/python/src')]
from app.runtime.database import Database
from dios_sdk import TaskClient


def qualify(directory, image):
    root = directory.resolve()
    if not root.is_relative_to(ROOT / 'workspace') or root.exists():
        raise ValueError('Use a new isolated directory under repository workspace')
    root.mkdir(mode=0o700)
    (root / 'profiles').mkdir()
    (root / 'object_server.py').write_text(SERVER)
    tokens = {key: secrets.token_urlsafe(32) for key in ('operator', 'a', 'b')}
    now = time.time()
    identity = {'version': 1, 'issuer': 'qualification:knowledge', 'audience': 'dios-runtime', 'principals': {}}
    for key in ('a', 'b'):
        identity['principals'][key] = {'subject': key, 'actor': key, 'scope': 'qualification',
            'enabled': True, 'revision': 1, 'expires_at': now + 1800,
            'tokens': [{'sha256': hashlib.sha256(tokens[key].encode()).hexdigest(), 'enabled': True,
                        'not_before': now - 1, 'expires_at': now + 1800}], 'profiles': {}, 'objects': {}}
    for filename, value in {
        'server.json': {'scope': 'qualification', 'token': tokens['operator']},
        'policy.json': {'version': 1, 'identity_catalog': str(root / 'principals.json')},
        'principals.json': identity,
    }.items():
        path = root / filename
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    async def initialize():
        db = Database('sqlite+aiosqlite:///' + str(root / 'state.sqlite'))
        try:
            await db.migrate()
        finally:
            await db.close()
    asyncio.run(initialize())
    docker_client = docker.from_env()
    container = None

    def stop():
        nonlocal container
        if container is not None:
            container.stop(timeout=10)
            (root / (container.short_id + '.log')).write_bytes(container.logs())
            container.remove()
            container = None

    def start(provider):
        nonlocal container
        container = docker_client.containers.run(image,
            ['python', '-m', 'uvicorn', 'object_server:app', '--host', '0.0.0.0', '--port', '8000'],
            detach=True, user=f'{os.getuid()}:{os.getgid()}', read_only=True, cap_drop=['ALL'],
            security_opt=['no-new-privileges'], ports={'8000/tcp': ('127.0.0.1', None)},
            volumes={str(root): {'bind': str(root), 'mode': 'rw'}},
            environment={'PYTHONPATH': '/app:' + str(root), 'QUALIFICATION_ROOT': str(root),
                         'DIOS_RUNTIME_KNOWLEDGE_PROVIDER': provider},
            tmpfs={'/tmp': 'rw,noexec,nosuid,size=32m'})
        container.reload()
        port = container.attrs['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort']
        base = 'http://127.0.0.1:' + port
        with httpx.Client(trust_env=False) as http:
            for _ in range(100):
                try:
                    response = http.get(base + '/api/os/runtime/knowledge/capabilities',
                                        headers={'Authorization': 'Bearer ' + tokens['operator']})
                    if response.status_code == 200:
                        assert response.json()['provider'] == provider
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('Private qualification API did not become ready')
        return {key: TaskClient(base, token) for key, token in tokens.items()}

    try:
        clients = start('object-read-through-v1')
        operator = clients['operator']
        refs = []
        for key in ('a', 'b'):
            ref = operator.publish_file(('共同 知识契约 ' + key + ' alpha "literal" Straße').encode(),
                                        idempotency_key='source-' + key)['ref']
            refs.append(ref)
            operator.set_object_policy('file', ref['id'], [{'principal_id': key,
                'actions': ['object.read', 'object.download'], 'expires_at': now + 1800}],
                expected_revision=0, idempotency_key='grant-' + key)
            operator.replace_knowledge_source(key, ref,
                {'namespace': 'fixture', 'key': key, 'version': 'v1', 'locator': 'opaque/1'},
                expected_revision=0, idempotency_key='bind-' + key)
        queries = ['共同', '知识契约', 'alpha', '"literal"', 'STRASSE', 'unknown']
        baseline = {(key, query): clients[key].search_knowledge(query)['items']
                    for key in ('a', 'b') for query in queries}
        for key in ('a', 'b'):
            assert baseline[key, '共同'][0]['object_ref'] == refs[0 if key == 'a' else 1]
        stop()
        clients = start('sqlite-fts5-v1')
        operator = clients['operator']
        for (key, query), items in baseline.items():
            response = clients[key].search_knowledge(query)
            assert response['items'] == items and response['provider'] == 'sqlite-fts5-v1'
        latest = operator.publish_file('新版本 alpha'.encode(), idempotency_key='v2',
            object_id=refs[0]['id'], expected_version=1)['ref']
        provenance = {'namespace': 'fixture', 'key': 'a', 'version': 'v2', 'locator': 'opaque/2'}
        receipt = operator.replace_knowledge_source('a', latest, provenance,
                                                    expected_revision=1, idempotency_key='replace')
        assert clients['a'].search_knowledge('alpha')['items'] == []
        operator.set_object_policy('file', latest['id'], [{'principal_id': 'a',
            'actions': ['object.read', 'object.download'], 'expires_at': now + 1800}],
            version=2, expected_revision=0, idempotency_key='grant-v2')
        assert clients['a'].search_knowledge('alpha')['items'][0]['object_ref'] == latest
        operator.delete_knowledge_source('a', expected_revision=2, idempotency_key='delete')
        assert clients['a'].search_knowledge('alpha')['items'] == []
        caps = operator.knowledge_capabilities()
        stop()
        clients = start('object-read-through-v1')
        replay = clients['operator'].replace_knowledge_source('a', latest, provenance,
                                                              expected_revision=1, idempotency_key='replace')
        assert replay == receipt
        assert clients['operator'].knowledge_source('a')['state'] == 'deleted'
        assert clients['a'].search_knowledge('alpha')['items'] == []
        assert clients['b'].search_knowledge('alpha')['items'][0]['object_ref'] == refs[1]
        report = {'passed': True, 'image': image, 'same_installation': True, 'principals': 2,
                  'switch_order': ['object-read-through-v1', 'sqlite-fts5-v1', 'object-read-through-v1'],
                  'query_comparisons': len(baseline), 'source_refs': refs, 'updated_ref': latest,
                  'version_grant_required': True, 'delete_and_old_receipt_replay_preserved': True,
                  'fts_capabilities': caps, 'new_model_calls': 0}
        (root / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
    finally:
        stop()
        docker_client.close()
        for name in ('server.json', 'principals.json'):
            (root / name).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    qualify(args.directory, args.image)
