"""Standard-library HTTPS/mTLS probe. Private inputs arrive on stdin, never argv."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile


def main():
    value = json.load(sys.stdin)
    result = {'pid': os.getpid(), 'boot_sha256': hashlib.sha256(
        Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix='dios-identity-probe-') as directory:
        root = Path(directory)
        for name in ('ca', 'certificate', 'key'):
            path = root / name
            path.write_text(value[name]);path.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cafile=str(root/'ca'))
        context.load_cert_chain(str(root/'certificate'), str(root/'key'))
        if value['mode'] == 'rpc':
            context.set_alpn_protocols(['dios-worker-tools/1'])
        try:
            with socket.create_connection(('127.0.0.1', value['port']), 8) as raw:
                with context.wrap_socket(raw, server_hostname=value.get('server_name','localhost')) as connection:
                    result.update(tls=connection.version(),cipher=connection.cipher()[0])
                    if value['mode'] == 'rpc':
                        connection.sendall(json.dumps(value['envelope']).encode()+b'\n')
                        with connection.makefile('rb') as stream:body=stream.readline(2*1024*1024+1)
                        if len(body)>2*1024*1024:raise ValueError('response limit')
                        result['response']=json.loads(body)
                    else:
                        path=value['path'];token=value['token']
                        if any(c in path+token for c in '\r\n'):raise ValueError('invalid request')
                        connection.sendall(('GET '+path+' HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+'\r\nConnection: close\r\n\r\n').encode())
                        response=http.client.HTTPResponse(connection);response.begin()
                        body=response.read(2*1024*1024+1)
                        if len(body)>2*1024*1024:raise ValueError('response limit')
                        result.update(status=response.status,sha256=hashlib.sha256(body).hexdigest(),size=len(body),cache_control=response.getheader('cache-control'))
        except ssl.SSLCertVerificationError:
            result['error']='TLS_CERTIFICATE_REJECTED'
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()