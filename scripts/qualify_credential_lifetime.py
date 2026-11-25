"""Real 900-second grant/credential renewal over local mTLS, without time mocks.

Controlled task/session fixture; no Agent/model launch. Completes after the first
natural renewal (about ten minutes), preserving an evidence ledger without keys.
"""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests')]
from sqlalchemy import select
from app.runtime.database import RuntimeErrorCode
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.worker_credentials import WorkerCredentials
from app.runtime.worker_identity import tls_context
from app.runtime.work_auth_schema import work, grants, decisions
import test_runtime_remote_tools as fixtures


async def qualify(output, *, edges_only=False):
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'workspace') or output.exists():
        raise ValueError('Use a new workspace output file')
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixture = fixtures.RemoteToolTests()
    await fixture.asyncSetUp()
    f = fixture.f
    root = f.root / 'tls'
    root.mkdir(mode=0o700)
    stop = asyncio.Event()
    server = renewal = None
    report = {'passed': False, 'mode': 'controlled task; real mTLS and wall-clock; no model',
              'edges_only': edges_only, 'checks': []}
    started = time.monotonic()
    def write():
        output.write_text(json.dumps(report, indent=2) + '\n')
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report['checks'].append(name)
        print(name, flush=True)
        write()
    def certificates():
        def command(*args):
            subprocess.run(['openssl', *args], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        command('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.pem', '-subj', '/CN=DiOS qualification CA', '-days', '1')
        for name, usage in [('server', 'serverAuth'), ('client', 'clientAuth')]:
            command('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', name + '.key', '-out', name + '.csr', '-subj', '/CN=' + name)
            (root / (name + '.ext')).write_text('extendedKeyUsage=' + usage + '\nsubjectAltName=DNS:localhost\n')
            command('x509', '-req', '-in', name + '.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial',
                    '-out', name + '.pem', '-days', '1', '-extfile', name + '.ext')
    async def state():
        async with f.db.read(f.scope) as c:
            w = dict((await c.execute(select(work).where(work.c.run_id == f.run['id']))).mappings().one())
            g = dict((await c.execute(select(grants).where(grants.c.attempt_id == f.attempt['id']))).mappings().one())
            now = await f.db.now(c)
        return w, g, now
    try:
        await asyncio.to_thread(certificates)
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert((root / 'client.pem').read_text())).hexdigest()
        machine = {k: v for k, v in fixture.row.items() if k != 'token_sha256'}
        machine.update(certificate_sha256=fingerprint, issuance_enabled=True, revision=1, expires_at=time.time() + 7200)
        fixture.identities_file.write_text(json.dumps({'version': 1, 'audience': 'tools:test', 'machines': [machine]}))
        issuer_key = root / 'issuer.key'
        issuer_key.write_bytes(secrets.token_bytes(32))
        issuer_key.chmod(0o600)
        fixture.gateway.credentials = WorkerCredentials(f.store, fixture.identities, f.policy, issuer_key)
        context = tls_context(str(root / 'ca.pem'), str(root / 'server.pem'), str(root / 'server.key'), server=True)
        server = asyncio.create_task(fixture.gateway.serve('127.0.0.1', 0, context, stop))
        async with asyncio.timeout(5):
            while fixture.gateway.server is None:
                await asyncio.sleep(.01)
        port = fixture.gateway.server.sockets[0].getsockname()[1]
        config = dict(version=1, enabled=True, worker_id='worker', audience='tools:test', host='127.0.0.1',
            server_name='localhost', port=port, ca_file=str(root / 'ca.pem'), certificate_file=str(root / 'client.pem'),
            key_file=str(root / 'client.key'), credential_mode='automatic')
        path = root / 'client.json'
        path.write_text(json.dumps(config))
        provider = RemoteToolProvider(path, f.store, 'test-profile', fixture.spec)
        await provider.request(f.binding, {'method': 'describe', 'params': {}})
        first = dict(provider.credentials[tuple(f.binding)]['value'])
        w0, g0, now = await state()
        check('default-credential-lifetime-900s', 899 <= first['expires_at'] - first['server_time'] <= 900.01)
        check('initial-execution-grant-900s', 890 <= g0['expires_at'] - now <= 900)
        report.update(run_id=f.run['id'], attempt_id=f.attempt['id'], process_id=os.getpid(),
            original_work_deadline=w0['expires_at'], initial_grant_deadline=g0['expires_at'],
            initial_credential_deadline=first['expires_at'], initial_credential_version=first['version'])
        write()
        renewal = asyncio.create_task(provider.keep_credential(f.binding))
        last = -1
        while time.monotonic() - started < 680:
            await f.store.heartbeat(*f.binding)
            await f.verifier(replace(f.context, session_id='session-one'), 'execute')
            w, grant, now = await state()
            current = provider.credentials[tuple(f.binding)]['value']
            assert w['expires_at'] == w0['expires_at'] and grant['expires_at'] <= w0['expires_at']
            assert current['expires_at'] <= w0['expires_at']
            if edges_only:
                break
            minute = int((time.monotonic() - started) // 60)
            if minute != last:
                last = minute
                report['observation'] = dict(elapsed_seconds=round(time.monotonic() - started, 2),
                    grant_version=grant['version'], credential_version=current['version'],
                    owner_live=True, renewal_loop_live=not renewal.done())
                write()
                print(json.dumps(report['observation']), flush=True)
            if current['version'] > first['version'] and grant['expires_at'] > g0['expires_at']:
                check('natural-900s-grant-renewal', now >= g0['expires_at'] - 300)
                check('automatic-credential-renewal-over-mtls', current['access_token'] != first['access_token'])
                check('original-work-deadline-preserved', w['expires_at'] == w0['expires_at'])
                report.update(renewed_grant_deadline=grant['expires_at'], renewed_credential_deadline=current['expires_at'],
                    renewed_grant_version=grant['version'], renewed_credential_version=current['version'])
                break
            await asyncio.sleep(2)
        else:
            raise AssertionError('natural renewal did not occur before 680 seconds')
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        # Two different nonces compete for the same actual issued token version.
        async def exchange():
            return await provider._wire(f.binding, {'method': 'credentials.renew', 'params': {
                'nonce': secrets.token_urlsafe(32), 'expected_version': current['version']}}, current['access_token'])
        outcomes = await asyncio.gather(exchange(), exchange(), return_exceptions=True)
        # Gateway may reject concurrent admission before the version check; retry
        # that known-not-admitted attempt to observe the durable version conflict.
        for index, outcome in enumerate(outcomes):
            if isinstance(outcome, RuntimeErrorCode) and outcome.code == 'WORKER_GATEWAY_BUSY':
                try:
                    outcomes[index] = await exchange()
                except RuntimeErrorCode as exc:
                    outcomes[index] = exc
        check('concurrent-version-conflict', sum(isinstance(x, dict) for x in outcomes) == 1 and
              any(isinstance(x, RuntimeErrorCode) and x.code == 'VERSION_CONFLICT' for x in outcomes))
        latest = next(x for x in outcomes if isinstance(x, dict))
        async def denied(code, binding=None):
            try:
                await provider._wire(binding or f.binding, {'method': 'credentials.renew', 'params': {
                    'nonce': secrets.token_urlsafe(32), 'expected_version': latest['version']}}, latest['access_token'])
            except RuntimeErrorCode as exc:
                check(code, exc.code == code)
            else:
                raise AssertionError('renewal unexpectedly allowed: ' + code)
        await denied('STALE_OWNER', (*f.binding[:3], 'changed-owner', f.binding[4]))
        policy_before = f.policy.path.read_text()
        policy = json.loads(policy_before)
        caller = policy['profiles']['test-profile']['permissions']['caller']
        narrowed = [permission for permission in caller if permission['action'] != 'repository.search']
        assert narrowed and len(narrowed) < len(caller)
        policy['profiles']['test-profile']['permissions']['caller'] = narrowed
        f.policy.path.write_text(json.dumps(policy))
        await denied('WORK_POLICY_CHANGED')
        f.policy.path.write_text(policy_before)
        saved = await f.store.get(f.run['id'])
        await f.store.cancel(saved['id'], saved['version'])
        await denied('AUTH_REVOKED')
        async with f.db.read(f.scope) as c:
            audit = (await c.execute(select(decisions.c.decision).where(decisions.c.run_id == f.run['id']))).scalars().all()
        serialized = json.dumps([await f.store.get(f.run['id']), audit, await fixture.gateway.credentials.status(f.run['id'])])
        check('tokens-absent-from-task-audit-and-status', all(x['access_token'] not in serialized for x in [first, current, latest]))
        report.update(passed=True, elapsed_seconds=round(time.monotonic() - started, 3), new_model_calls=0)
        write()
    finally:
        if renewal:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        stop.set()
        if server:
            await asyncio.wait_for(server, 15)
        await fixture.asyncTearDown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--edges-only', action='store_true', help='Run the mTLS rejection matrix without repeating the natural lifetime wait')
    args = parser.parse_args()
    asyncio.run(qualify(args.output, edges_only=args.edges_only))
