"""Native OpenBao Agent templates -> pinned Secret resolver -> governed Docker Worker."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'backend/tests')]
from app.runtime.adapter import LaunchSpec
from app.runtime.docker_adapter import DockerRuntimeAdapter
from test_runtime_secret_partition import SecretPartitionTests
from probe_secret_provider import IMAGE


def private(path,value):path.write_text(value);path.chmod(0o600)


async def worker_run(store_config,path,version,value,*,error=None):
    fixture=SecretPartitionTests();await fixture.asyncSetUp()
    adapter=DockerRuntimeAdapter(fixture.scope)
    try:
        code='''import hashlib,json,os
from pathlib import Path
p=Path('/run/secrets/model');raw=p.read_bytes()
assert not Path('/run/secrets/gateway').exists()
assert not any(raw.decode() in v for v in os.environ.values())
try: p.write_bytes(b'changed')
except OSError: pass
else: raise AssertionError('writable secret')
print(json.dumps({'type':'session','session_id':'native-secret'}),flush=True)
print(json.dumps({'type':'result','session_id':'native-secret','result':{
 'summary':'native versioned secret read-only; gateway isolated',
 'evidence':['sha256:'+hashlib.sha256(raw).hexdigest()]}}),flush=True)
'''
        image=adapter.client.images.get('python:3.12-slim').id
        spec=LaunchSpec(image,('python','-c',code),user='65534:65534')
        await fixture.prepare(['model'],spec=spec,version=f'v{version}')
        catalog=fixture.resolver.path;config=json.loads(catalog.read_text())
        config['secret_store']=store_config
        config['secrets']['model'].update(path=str(path),sha256=hashlib.sha256(value.encode()).hexdigest(),
            secret_ref={'mount':'secret','path':'model','field':'value','version':version})
        private(catalog,json.dumps(config))
        result=await fixture.worker(adapter).once()
        if error:
            assert result['state']=='failed',result['state']
            assert result['attempts'][0]['error']['code']==error,result['attempts'][0]['error']['code']
            assert result['attempts'][0].get('process_id') is None
        else:
            assert result['state']=='succeeded',result['attempts'][0].get('error')
            assert value not in json.dumps(result)
            for output in (fixture.root/'artifacts').rglob('*'):
                if output.is_file():assert value.encode() not in output.read_bytes()
        return {'version':version,'state':result['state'],'error':error,'run_id':result['id'],'image':image}
    finally:
        for container in adapter.client.containers.list(all=True,filters={'label':'dios.runtime.scope='+adapter.scope}):container.remove(force=True)
        adapter.client.close();await fixture.asyncTearDown()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--port',type=int,default=13289)
    args=parser.parse_args();base=f'http://127.0.0.1:{args.port}';checks=[];containers=[];workers=[]
    def docker(*argv):return subprocess.check_output(['docker',*argv],text=True,stderr=subprocess.PIPE).strip()
    def mark(name):checks.append(name);print(name,flush=True)
    with tempfile.TemporaryDirectory(prefix='dios-secret-injection-') as directory,httpx.Client(base_url=base,trust_env=False,timeout=2) as http:
        root=Path(directory);(root/'data').mkdir();admin={}
        def request(method,path,body=None,token=None):
            return http.request(method,'/v1/'+path,json=body,headers={'X-Vault-Token':token or admin.get('token','')},
                timeout=30 if path in ('sys/init','sys/unseal') else 2)
        def ok(method,path,body=None):
            response=request(method,path,body);assert response.is_success,(method,path,response.status_code)
            return response.json() if response.content else {}
        def wait(fn,seconds=40):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                try:
                    if value:=fn():return value
                except (httpx.HTTPError,FileNotFoundError):pass
                time.sleep(.2)
            raise AssertionError('Native provider did not become ready')
        private(root/'server.json',json.dumps({'storage':{'raft':{'path':'/fixture/data','node_id':'injection'}},
            'listener':{'tcp':{'address':f'127.0.0.1:{args.port}','tls_disable':True}},'api_addr':base,'cluster_addr':f'http://127.0.0.1:{args.port+1}'}))
        def start(*command):
            cid=docker('run','-d','--network','host','--user',f'{os.getuid()}:{os.getgid()}',
                '--memory','512m','--label','dios.purpose=secret-injection-qualification','-v',str(root)+':/fixture',IMAGE,*command)
            containers.append(cid);return cid
        def agent(version):
            directory=root/f'agent-{version}';directory.mkdir(mode=0o700)
            private(directory/'role',ok('GET','auth/approle/role/reader/role-id')['data']['role_id'])
            private(directory/'secret',ok('POST','auth/approle/role/reader/secret-id')['data']['secret_id'])
            prefix=f'/fixture/agent-{version}'
            template='{{- with secret "secret/data/model?version='+str(version)+'" -}}{{ .Data.data.value }}{{- end -}}'
            config={'vault':{'address':base},'auto_auth':{'method':[{'type':'approle','config':{
                'role_id_file_path':prefix+'/role','secret_id_file_path':prefix+'/secret','remove_secret_id_file_after_reading':False}}],
                'sink':[{'type':'file','config':{'path':prefix+'/token','mode':384}}]},
                'template_config':{'exit_on_retry_failure':True,'static_secret_render_interval':'5m'},
                'template':[{'contents':template,'destination':prefix+'/model','perms':'0444','backup':False,'error_on_missing_key':True}]}
            private(directory/'agent.json',json.dumps(config));start('agent','-config='+prefix+'/agent.json')
            wait(lambda:(directory/'model').is_file() and (directory/'token').is_file())
            assert (directory/'secret').stat().st_mode&0o777==0o600
            return directory
        try:
            server=start('server','-config=/fixture/server.json')
            wait(lambda:http.get('/v1/sys/health').status_code==501)
            init=ok('POST','sys/init',{'secret_shares':1,'secret_threshold':1});admin['token']=init['root_token']
            ok('POST','sys/unseal',{'key':init['keys_base64'][0]});wait(lambda:http.get('/v1/sys/health').status_code==200)
            ok('POST','sys/mounts/secret',{'type':'kv','options':{'version':'2'}})
            wait(lambda:request('GET','secret/config').status_code==200)
            first='synthetic-v1-'+secrets.token_hex(16);second='synthetic-v2-'+secrets.token_hex(16)
            ok('POST','secret/data/model',{'options':{'cas':0},'data':{'value':first}})
            ok('PUT','sys/policies/acl/reader',{'policy':'path "secret/data/model" { capabilities = ["read"] }'})
            ok('POST','sys/auth/approle',{'type':'approle'})
            ok('POST','auth/approle/role/reader',{'token_policies':['reader'],'token_ttl':'1m','token_max_ttl':'5m','secret_id_ttl':'10m'})
            a1=agent(1);assert (a1/'model').read_text()==first
            assert (a1/'model').stat().st_mode&0o777==0o444
            assert (a1/'token').stat().st_mode&0o777==0o600
            token=(a1/'token').read_text().strip()
            for method,path,body in [('GET','secret/data/other',None),('POST','secret/data/model',{'data':{'value':'denied'}}),('DELETE','secret/data/model',None)]:
                assert request(method,path,body,token).status_code==403
            mark('native-agent-private-sink-read-only-policy-and-template')
            store={'address':base,'token_file':str(a1/'token'),'timeout_seconds':.3}
            workers.append(asyncio.run(worker_run(store,a1/'model',1,first)));mark('e2ag-worker-native-v1-injection-no-secret-output')
            docker('restart',containers[-1])
            wait(lambda:(a1/'token').read_text().strip()!=token)
            mark('native-agent-restart-reauthenticates-from-private-bootstrap')
            ok('POST','secret/data/model',{'options':{'cas':1},'data':{'value':second}})
            a2=agent(2);assert (a2/'model').read_text()==second;assert (a1/'model').read_text()==first
            workers.append(asyncio.run(worker_run(store,a2/'model',2,second)));mark('new-version-new-file-new-work-old-version-preserved')
            # A still-present native render cannot authorize a deleted upstream version.
            ok('POST','secret/delete/model',{'versions':[1]});assert (a1/'model').read_text()==first
            workers.append(asyncio.run(worker_run(store,a1/'model',1,first,error='SECRET_UNAVAILABLE')));mark('deleted-version-stale-file-rejected-before-launch')
            docker('pause',server)
            workers.append(asyncio.run(worker_run(store,a2/'model',2,second,error='SECRET_PROVIDER_UNAVAILABLE')))
            docker('unpause',server);mark('service-outage-fails-closed-with-local-cache-present')
            workers.append(asyncio.run(worker_run(store,a2/'model',2,second)));mark('service-recovery-new-work-uses-pinned-v2')
            protected=[first,second,token,admin['token'],init['keys_base64'][0]]
            protected += [(directory/name).read_text().strip() for directory in (a1,a2) for name in ('secret','token')]
            for cid in containers:
                captured=subprocess.run(['docker','logs',cid],capture_output=True,text=True,check=True)
                logs=captured.stdout+captured.stderr
                assert all(value not in logs for value in protected)
            mark('native-service-logs-contain-no-secret-values')
            report={'passed':True,'image':IMAGE,'checks':checks,'workers':workers,'credentials':'synthetic only',
                'native_renewal_evidence':'docs/design/evidence/managed-secrets-20261124.json',
                'limits':['Single Linux host; external TLS and cross-host qualification remain separate.',
                    'Rotation uses new version/files and new Work; active workload drain uses existing lifecycle control.']}
            Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
        except BaseException:
            for n,cid in enumerate(containers):
                logs=subprocess.run(['docker','logs',cid],capture_output=True,text=True)
                private(Path(args.output+f'.diagnostic-{n}'),logs.stdout+logs.stderr)
            raise
        finally:
            for cid in reversed(containers):subprocess.run(['docker','rm','-f',cid],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':main()
