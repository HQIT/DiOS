"""Isolated native Keycloak key/client-secret rotation and DiOS revocation."""
import argparse
import asyncio
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs

import httpx
from probe_identity_provider import IMAGE

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests')]
from app.runtime.database import RuntimeErrorCode
from app.runtime.logout_verifier import verify
from app.runtime.logout_events import receive
from app.runtime.work_authorizations import view
import test_runtime_oauth_source as fixtures


async def qualify(output, binary):
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'workspace') or output.exists():
        raise ValueError('Use a new workspace output file')
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    received = queue.Queue()
    class Capture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            data = self.rfile.read(min(int(self.headers['Content-Length']), 32768))
            received.put(parse_qs(data.decode())['logout_token'][0])
            self.send_response(200)
            self.end_headers()
    callback = ThreadingHTTPServer(('127.0.0.1', 0), Capture)
    thread = threading.Thread(target=callback.serve_forever, daemon=True)
    thread.start()
    container = None
    fixture = None
    report = {'passed': False, 'image': IMAGE, 'checks': [], 'new_model_calls': 0}
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report['checks'].append(name)
        output.write_text(json.dumps(report, indent=2) + '\n')
        print(name, flush=True)
    def docker(*args):
        return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE).strip()
    def kid(token):
        return json.loads(base64.urlsafe_b64decode(token.split('.')[0] + '==='))['kid']
    try:
        with tempfile.TemporaryDirectory(prefix='dios-native-rotation-') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            realm_name = 'dios-rotation'
            issuer = base + '/realms/' + realm_name
            password, admin_password, api_secret = [secrets.token_urlsafe(32) for _ in range(3)]
            realm = dict(realm=realm_name, enabled=True, sslRequired='none', accessTokenLifespan=900,
                users=[dict(username='synthetic-user', enabled=True, emailVerified=True, firstName='Test', lastName='User',
                            email='test@example.invalid', credentials=[dict(type='password', value=password, temporary=False)])],
                clients=[dict(clientId='portal', publicClient=True, directAccessGrantsEnabled=True, standardFlowEnabled=False,
                    attributes={'backchannel.logout.url': f'http://127.0.0.1:{callback.server_port}/logout',
                                'backchannel.logout.session.required': 'true'},
                    protocolMappers=[dict(name='resource-audience', protocol='openid-connect', protocolMapper='oidc-audience-mapper',
                        config={'included.client.audience': 'dios-api', 'access.token.claim': 'true', 'introspection.token.claim': 'true'})]),
                    dict(clientId='dios-api', publicClient=False, secret=api_secret, standardFlowEnabled=False)])
            imports = root / 'import'
            imports.mkdir(mode=0o755)
            (imports / 'realm.json').write_text(json.dumps(realm))
            env = root / 'env'
            env.write_text('KC_BOOTSTRAP_ADMIN_USERNAME=qualification-admin\nKC_BOOTSTRAP_ADMIN_PASSWORD=' + admin_password + '\n')
            env.chmod(0o600)
            container = await asyncio.to_thread(docker, 'run', '-d', '--network', 'host', '--memory=1g', '--cpus=2',
                '--label', 'dios.purpose=isolated-identity-rotation', '--env-file', str(env),
                '-v', str(imports) + ':/opt/keycloak/data/import:ro', IMAGE, 'start-dev', '--import-realm',
                '--hostname', base, '--http-host', '127.0.0.1', '--http-port', str(port))
            with httpx.Client(timeout=10, trust_env=False) as http:
                for _ in range(180):
                    try:
                        if http.get(issuer + '/.well-known/openid-configuration').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.5)
                else:
                    raise AssertionError('isolated-provider-start-timeout')
                response = http.post(base + '/realms/master/protocol/openid-connect/token', data=dict(
                    client_id='admin-cli', grant_type='password', username='qualification-admin', password=admin_password))
                response.raise_for_status()
                admin = {'Authorization': 'Bearer ' + response.json()['access_token']}
                prefix = base + '/admin/realms/' + realm_name
                def admin_request(method, suffix, **kwargs):
                    result = http.request(method, prefix + suffix, headers=admin, **kwargs)
                    result.raise_for_status()
                    return result
                def login():
                    result = http.post(issuer + '/protocol/openid-connect/token', data=dict(client_id='portal',
                        grant_type='password', username='synthetic-user', password=password, scope='openid'))
                    result.raise_for_status()
                    return result.json()
                def logout(token):
                    result = http.post(issuer + '/protocol/openid-connect/logout',
                        data={'client_id': 'portal', 'refresh_token': token['refresh_token']})
                    result.raise_for_status()
                    return received.get(timeout=10)
                original = login()
                access = original['access_token']
                source_path = root / 'access'
                source_path.write_text(access)
                source_path.chmod(0o600)
                secret_path = root / 'client-secret'
                secret_path.write_text(api_secret)
                secret_path.chmod(0o600)
                response = http.post(issuer + '/protocol/openid-connect/token/introspect', auth=('dios-api', api_secret), data={'token': access})
                response.raise_for_status()
                claims = response.json()
                check('native-source-active', claims['active'])
                provider = dict(issuer=issuer, audience='dios-api', endpoint=issuer + '/protocol/openid-connect/token/introspect',
                    client_id='dios-api', client_secret_file=str(secret_path), timeout_seconds=2)
                reference = dict(ref='rotation/source', version='v1', path=str(source_path), sha256=hashlib.sha256(access.encode()).hexdigest(),
                    issuer=issuer, audience='dios-api', subject=claims['sub'], client_id='portal', expires_at=claims['exp'], session_id=claims['sid'])
                fixture = fixtures.OAuthPrincipalTests()
                def configure(_):
                    fixture.access = access
                    fixture.config = provider
                    fixture.reference = reference
                    fixture.expiry = claims['exp']
                    fixture.transport = None
                fixture.configure = configure
                await fixture.asyncSetUp()
                run = await fixture.submit_as('a')
                context = await fixture.context(run)
                await fixture.verifier(context, 'execute')
                before = await view(fixture.console.store, run['id'])
                # Regenerate the real introspection client's secret, then replace
                # the private reference without changing source or Work identity.
                client_id = admin_request('GET', '/clients', params={'clientId': 'dios-api'}).json()[0]['id']
                new_secret = admin_request('POST', '/clients/' + client_id + '/client-secret').json()['value']
                check('client-secret-rotated', new_secret != api_secret)
                try:
                    await fixture.verifier(context, 'execute')
                except RuntimeErrorCode as exc:
                    check('old-client-secret-is-unavailable-not-user-revoked', exc.code == 'AUTH_PROVIDER_UNAVAILABLE')
                else:
                    raise AssertionError('old client secret accepted')
                check('credential-outage-retains-work', (await view(fixture.console.store, run['id']))['state'] == 'active')
                secret_path.write_text(new_secret)
                await fixture.verifier(context, 'execute')
                after = await view(fixture.console.store, run['id'])
                check('new-secret-resumes-original-work', after['reference'] == before['reference'] and after['expires_at'] == before['expires_at'])
                config = root / 'logout.json'
                config.write_text(json.dumps({'version': 1, 'providers': {'native': dict(issuer=issuer, client_id='portal',
                    jwks_uri=issuer + '/protocol/openid-connect/certs', algorithms=['RS256'])}}))
                prior = login()
                old_logout = await asyncio.to_thread(logout, prior)
                old_event, _ = await verify(config, binary, 'native', old_logout)
                old_kid = kid(old_logout)
                check('original-key-native-logout-verified', old_event['subject'] == claims['sub'])
                keys = admin_request('GET', '/keys').json()
                old_provider = next(key['providerId'] for key in keys['keys'] if key['kid'] == old_kid)
                realm_id = admin_request('GET', '').json()['id']
                admin_request('POST', '/components', json=dict(name='qualification-rotation', parentId=realm_id,
                    providerId='rsa-generated', providerType='org.keycloak.keys.KeyProvider',
                    config={'priority': ['200'], 'enabled': ['true'], 'active': ['true'], 'algorithm': ['RS256'], 'keySize': ['2048']}))
                newer = login()
                new_kid = kid(newer['access_token'])
                check('native-new-signing-key-used', new_kid != old_kid)
                await fixture.verifier(context, 'execute')
                check('key-overlap-preserves-original-work', (await view(fixture.console.store, run['id']))['state'] == 'active')
                # Passive old keys remain valid during the explicit overlap.
                component = admin_request('GET', '/components/' + old_provider).json()
                component['config']['active'] = ['false']
                admin_request('PUT', '/components/' + old_provider, json=component)
                await verify(config, binary, 'native', old_logout)
                check('passive-old-key-valid-during-overlap', True)
                # Logout of the original source is now signed by the new key.
                new_logout = await asyncio.to_thread(logout, original)
                check('native-logout-uses-new-signing-key', kid(new_logout) == new_kid)
                event, hashed = await verify(config, binary, 'native', new_logout)
                receipt = await receive(fixture.console.store, event, hashed)
                check('new-key-logout-revokes-source-work', receipt['revoked_work_count'] == 1)
                try:
                    await fixture.verifier(context, 'execute')
                except RuntimeErrorCode as exc:
                    check('revoked-work-cannot-renew', exc.code in {'AUTH_REVOKED', 'AUTH_PRINCIPAL_REVOKED'})
                else:
                    raise AssertionError('revoked work executed')
                component['config']['enabled'] = ['false']
                admin_request('PUT', '/components/' + old_provider, json=component)
                try:
                    await verify(config, binary, 'native', old_logout)
                except RuntimeErrorCode as exc:
                    check('disabled-old-key-rejected', exc.code == 'LOGOUT_TOKEN_INVALID')
                else:
                    raise AssertionError('disabled signing key accepted')
                await verify(config, binary, 'native', new_logout)
                check('new-key-remains-valid-after-old-disabled', True)
                saved = await fixture.console.store.get(run['id'])
                check('secrets-absent-from-runtime-record', all(value not in json.dumps(saved) for value in [access, api_secret, new_secret, old_logout, new_logout]))
                report.update(passed=True, run_id=run['id'], source_session=claims['sid'], old_kid=old_kid, new_kid=new_kid,
                    event_id=receipt['event_id'], original_work_deadline=before['expires_at'],
                    verifier_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                    limits=['Isolated local Keycloak with synthetic user; direct password grant only bootstraps this fixture.',
                            'Enterprise IdP configuration, TLS and physical multihost qualification remain separately tracked.'])
                output.write_text(json.dumps(report, indent=2) + '\n')
    finally:
        if fixture:
            await fixture.asyncTearDown()
        if container:
            await asyncio.to_thread(docker, 'rm', '-f', container)
        await asyncio.to_thread(callback.shutdown)
        callback.server_close()
        thread.join(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--verifier', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(qualify(args.output, args.verifier.resolve()))
