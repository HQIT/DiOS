"""Synthetic filesystem and proxy-boundary checks inside a registered sandbox."""
import argparse
import errno
import json
import socket
from pathlib import Path


def proxy(request):
    with socket.create_connection(('model-egress', 3128), timeout=15) as connection:
        connection.sendall(request.encode('ascii'))
        status = connection.recv(4096).split(b'\r\n', 1)[0].decode('ascii')
        return int(status.split()[1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-host', default='api2.cursor.sh')
    host = parser.parse_args().model_host
    checks = {}
    for target in ['example.org:443', f'{host}.evil.invalid:443',
                   '127.0.0.1:443', '169.254.169.254:443', f'{host}:80']:
        status = proxy(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n')
        checks[target] = {'status': status, 'denied': status == 403}
        assert status == 403, (target, status)
    status = proxy(f'GET http://{host}/ HTTP/1.1\r\nHost: {host}\r\n\r\n')
    checks['non_connect'] = {'status': status, 'denied': status == 403}
    assert status == 403
    status = proxy(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n')
    checks['model_connect'] = {'status': status, 'allowed': status == 200}
    assert status == 200
    for host in ['1.1.1.1', '172.17.0.1']:
        try:
            with socket.create_connection((host, 443), timeout=2):
                raise AssertionError('Unexpected direct connection: ' + host)
        except OSError as exc:
            checks['direct:' + host] = {'blocked': True, 'errno': exc.errno}
    for name in ['/probe-source/sample.py', '/opt/dios/should-not-write']:
        try:
            with Path(name).open('a') as target:
                target.write('UNEXPECTED_SYNTHETIC_WRITE')
            raise AssertionError('Unexpected writable path: ' + name)
        except OSError as exc:
            assert exc.errno in {errno.EROFS, errno.EACCES}, (name, exc.errno)
            checks[name] = {'write_blocked': True, 'errno': exc.errno,
                            'control': 'read_only_mount' if exc.errno == errno.EROFS else 'filesystem_permissions'}
    print(json.dumps({'passed': True, 'checks': checks}))


if __name__ == '__main__':
    main()
