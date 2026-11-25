"""Initialize a new Linux installation or inspect it without exposing credentials."""
import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


class InstallError(Exception):
    pass


def command(args):
    if args[0] == 'docker':
        args = ['docker', '--host', 'unix:///var/run/docker.sock', *args[1:]]
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise InstallError('DEPENDENCY_NOT_ON_PATH:' + args[0]) from None
    except subprocess.TimeoutExpired:
        raise InstallError('DEPENDENCY_TIMEOUT:' + args[0]) from None
    if result.returncode:
        # External output can include environment or registry credentials.
        raise InstallError('DEPENDENCY_FAILED:' + args[0])
    return result.stdout.strip()


def locked_image(value):
    raw = command(['docker', 'image', 'inspect', value, '--format', '{{.Id}}'])
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', raw):
        raise InstallError('IMAGE_ID_INVALID')
    return raw


def private_write(path, text):
    with path.open('x', encoding='utf-8') as target:
        os.chmod(path, 0o600)
        target.write(text)


def init(args):
    if any(os.environ.get(k) for k in ('DOCKER_HOST','DOCKER_CONTEXT')):
        raise InstallError('LOCAL_DOCKER_ENDPOINT_REQUIRED')
    root = args.root.absolute()
    if root.exists():
        raise InstallError('INSTALLATION_ALREADY_EXISTS')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(root)) or root.resolve() != root:
        raise InstallError('INSTALLATION_PATH_INVALID')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', args.name):
        raise InstallError('INSTALLATION_NAME_INVALID')
    if args.api_port == args.web_port or not all(1024 <= p <= 65535 for p in (args.api_port,args.web_port)):
        raise InstallError('INSTALLATION_PORT_INVALID')
    command(['docker', 'compose', 'version', '--short'])
    backend, frontend = locked_image(args.backend_image), locked_image(args.frontend_image)
    sock = Path('/var/run/docker.sock').stat()
    if not stat.S_ISSOCK(sock.st_mode):
        raise InstallError('LOCAL_DOCKER_SOCKET_REQUIRED')
    root.mkdir(mode=0o700, parents=True)
    for name in ('data', 'data/objects', 'workspace', 'config', 'config/profiles'):
        (root / name).mkdir(mode=0o700)
    shutil.copyfile(HERE / 'compose.yaml', root / 'compose.yaml')
    shutil.copytree(HERE.parents[1] / 'cli', root / 'cli', ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    values = {'COMPOSE_PROJECT_NAME':args.name,'DIOS_ROOT':str(root),'DIOS_BACKEND_IMAGE':backend,
              'DIOS_FRONTEND_IMAGE':frontend,'DIOS_UID':os.getuid(),'DIOS_GID':os.getgid(),
              'DIOS_DOCKER_GID':sock.st_gid,'DIOS_API_PORT':args.api_port,'DIOS_WEB_PORT':args.web_port}
    private_write(root / '.env', ''.join(f'{key}={value}\n' for key,value in values.items()))
    env = {'DIOS_DATABASE_URL':f'sqlite+aiosqlite:///{root}/data/service.sqlite',
           'DIOS_WORKSPACE_ROOT':f'{root}/workspace','DIOS_HOST_WORKSPACE_ROOT':f'{root}/workspace',
           'DIOS_SHARED_CLI_ROOT':f'{root}/cli','DIOS_HOST_SHARED_CLI_ROOT':f'{root}/cli',
           'DIOS_ACCESS_TOKEN':secrets.token_urlsafe(32),'DIOS_E2AG_MODE':'enforce',
           'DIOS_RUNTIME_CONSOLE_ENABLED':'true','DIOS_RUNTIME_SCHEDULES_ENABLED':'true',
           'DIOS_RUNTIME_DATABASE_URL':f'sqlite+aiosqlite:///{root}/data/runtime.sqlite',
           'DIOS_RUNTIME_SCOPE':args.name,'DIOS_RUNTIME_PROFILES_DIR':f'{root}/config/profiles',
           'DIOS_RUNTIME_ARTIFACTS_ROOT':f'{root}/data/objects',
           'DIOS_RUNTIME_POLICY_FILE':f'{root}/config/work-policy.json'}
    private_write(root / 'service.env', ''.join(f'{key}={value}\n' for key,value in env.items()))
    private_write(root / 'config/work-policy.json', json.dumps({'version':1,'policy_id':'installation-v1',
                  'subject':'operator:installation','actor':'service:installation','profiles':{}},indent=2)+'\n')
    print(json.dumps({'initialized':str(root),'images':{'backend':backend,'frontend':frontend},
                      'credentials_file':str(root / 'service.env'),'profiles':0}))


def env_file(path):
    return dict(line.split('=',1) for line in path.read_text().splitlines() if line and not line.startswith('#'))


def worker_unit(args):
    import grp
    import pwd
    root, backend = args.root.resolve(), args.backend.resolve()
    if not all(re.fullmatch(r'/[A-Za-z0-9_./-]+',str(p)) for p in (root,backend)):
        raise InstallError('INSTALLATION_PATH_INVALID')
    if not os.access(backend / '.venv/bin/python', os.X_OK):
        raise InstallError('WORKER_PYTHON_MISSING')
    config=env_file(root / '.env')
    values={'ROOT':str(root),'BACKEND':str(backend),'USER':pwd.getpwuid(int(config['DIOS_UID'])).pw_name,
            'GROUP':grp.getgrgid(int(config['DIOS_GID'])).gr_name,
            'DOCKER_GROUP':grp.getgrgid(int(config['DIOS_DOCKER_GID'])).gr_name}
    template=(HERE / 'dios-worker@.service').read_text()
    for key,value in values.items():
        template=template.replace('@'+key+'@',value)
    target=root / 'dios-worker@.service'
    private_write(target,template)
    print(json.dumps({'unit':str(target),'enabled':False,'started':False}))


def doctor(args):
    root = args.root.resolve()
    config = env_file(root / '.env')
    service = env_file(root / 'service.env')
    checks = []

    def check(name, ok, detail):
        checks.append({'name':name,'ok':bool(ok),'detail':detail})

    docker = shutil.which('docker')
    check('docker_path', docker is not None, docker or 'docker missing from current PATH')
    check('installation_root', config.get('DIOS_ROOT') == str(root), str(root))
    for name in ('.env','service.env','config/work-policy.json'):
        path=root/name
        check('private:'+name, not path.stat().st_mode & 0o077, 'requires owner-only permissions')
    collisions = [key for key,value in config.items() if key in os.environ and os.environ[key] != value]
    collisions += [key for key in ('COMPOSE_FILE','COMPOSE_PROFILES','COMPOSE_ENV_FILES') if os.environ.get(key)]
    check('compose_environment', not collisions, {'conflicting_variable_names':collisions})
    check('access_token', len(service.get('DIOS_ACCESS_TOKEN','')) >= 32, 'presence and minimum length only')
    check('e2ag', service.get('DIOS_E2AG_MODE') == 'enforce', 'enforce required')
    check('runtime_console', service.get('DIOS_RUNTIME_CONSOLE_ENABLED') == 'true', 'explicitly enabled')
    check('docker_endpoint', not any(os.environ.get(k) for k in ('DOCKER_HOST','DOCKER_CONTEXT')),
          'local /var/run/docker.sock; remote context requires a separate deployment profile')
    if docker:
        for name,operation in [('daemon', ['docker','info','--format','{{.ServerVersion}}']),
                               ('compose', ['docker','compose','version','--short'])]:
            try:
                check(name, True, command(operation))
            except InstallError as exc:
                check(name, False, str(exc))
        for name in ('DIOS_BACKEND_IMAGE','DIOS_FRONTEND_IMAGE'):
            try:
                check(name, locked_image(config[name]) == config[name], 'immutable local image')
            except InstallError as exc:
                check(name, False, str(exc))
    check('proxy_configuration', True, {'host_present':[k for k in ('HTTP_PROXY','HTTPS_PROXY','NO_PROXY') if os.environ.get(k)],
                                       'service_present':[k for k in ('HTTP_PROXY','HTTPS_PROXY','NO_PROXY') if service.get(k)],
                                       'note':'Host environment is not inherited by services; Docker daemon proxy is configured separately.'})
    if args.running:
        url='http://127.0.0.1:'+config['DIOS_API_PORT']
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for name,endpoint in [('health','/health'),('runtime_profiles','/api/os/runtime/profiles')]:
            try:
                request=urllib.request.Request(url+endpoint,headers={'X-DiOS-Access-Token':service['DIOS_ACCESS_TOKEN']})
                with opener.open(request,timeout=5) as response:
                    value=json.load(response)
                check(name, value.get('status')=='ok' if name=='health' else isinstance(value.get('items'),list), 'API response checked')
            except (OSError,ValueError):
                check(name, False, 'API unavailable or response invalid')
    passed=all(item['ok'] for item in checks)
    print(json.dumps({'passed':passed,'checks':checks},ensure_ascii=False))
    return 0 if passed else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    create=sub.add_parser('init')
    create.add_argument('--root',type=Path,required=True)
    create.add_argument('--name',default='dios')
    create.add_argument('--backend-image',required=True)
    create.add_argument('--frontend-image',required=True)
    create.add_argument('--api-port',type=int,default=8000)
    create.add_argument('--web-port',type=int,default=3000)
    inspect=sub.add_parser('doctor')
    inspect.add_argument('--root',type=Path,required=True)
    inspect.add_argument('--running',action='store_true')
    unit=sub.add_parser('worker-unit')
    unit.add_argument('--root',type=Path,required=True)
    unit.add_argument('--backend',type=Path,required=True)
    args=parser.parse_args()
    try:
        if sys.platform != 'linux':
            raise InstallError('LINUX_REQUIRED')
        if args.command=='init':return init(args)
        if args.command=='worker-unit':return worker_unit(args)
        return doctor(args)
    except (InstallError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,InstallError) else 'INSTALLATION_CONFIGURATION_INVALID'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
