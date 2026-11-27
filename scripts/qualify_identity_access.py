"""Actual TLS clients exercise object ACL and E2AG tool dispatch from another host.

--local qualifies the harness only; physical acceptance requires a distinct Linux
boot identity over an existing, host-key-verified SSH alias. Synthetic data only.
"""
import argparse
import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'backend/tests')]
from test_runtime_principals import PrincipalTests
from test_runtime_worker import WorkerTests
from test_remote_recovery_qualification import RemoteRecoveryQualificationTests
from work_auth_fixtures import policy_fixture
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.store import digest
from app.runtime.worker import read_profile
from app.runtime.worker_identity import WorkerIdentities,tls_context,PROTOCOL
from app.runtime.remote_tool_gateway import ToolGateway
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.tool_catalog import FileToolCatalog,FileApprovers
from app.runtime.mcp_tool_transport import McpHttpTransport
from app.runtime.work_authorizations import WorkAuthorizationVerifier


class CaptureErrors(logging.Handler):
    def __init__(self):super().__init__(logging.ERROR);self.messages=[]
    def emit(self,record):self.messages.append(record.getMessage())


def write(path,value):
    path.write_text(json.dumps(value));path.chmod(0o600)


class WireRelay:
    """Observe TLS ciphertext without terminating TLS or exporting session keys."""
    def __init__(self,target):
        self.target=target;self.data=bytearray();self.tasks=set();self.server=None
    async def open(self):
        self.server=await asyncio.start_server(self.connection,'127.0.0.1',0)
        return self.server.sockets[0].getsockname()[1]
    async def connection(self,reader,writer):
        task=asyncio.current_task();self.tasks.add(task);up=None
        async def copy(source,destination):
            while chunk:=await source.read(65536):
                if len(self.data)+len(chunk)>8*1024*1024:raise ValueError('capture limit')
                self.data.extend(chunk);destination.write(chunk);await destination.drain()
            destination.close()
        try:
            source,up=await asyncio.open_connection('127.0.0.1',self.target)
            await asyncio.gather(copy(reader,up),copy(source,writer))
        except (ConnectionError,OSError):pass
        finally:
            writer.close()
            if up:up.close()
            self.tasks.discard(task)
    async def close(self):
        if self.server:self.server.close();await self.server.wait_closed()
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*list(self.tasks),return_exceptions=True)


async def prepare_tool(root):
    f=WorkerTests();await f.asyncSetUp()
    try:
        f.mcp=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'backend/tests/runtime_mcp_revocation_fixture.py'),str(root),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        endpoint=json.loads(await asyncio.wait_for(f.mcp.stdout.readline(),8))
        registration=dict(enabled=True,transport='mcp-http',url=f"http://127.0.0.1:{endpoint['port']}/mcp",name='read',approval_required=True,input_schema={'type':'object','additionalProperties':False},result_schema={'type':'object'})
        permission=dict(action='fixture.read',resource_kind='mcp_tool',resource_id='qualification/read',resource_version=digest({k:v for k,v in registration.items() if k!='enabled'}),constraint_revision='test-policy')
        registration['permission']=permission
        profile=f.root/'profile.json';write(profile,dict(id='test-profile',bridge='jsonl-v1',governance={'mode':'e2ag'},tool_channel={'tools':{'read':'read'}},launch={'image':'qualification-no-agent-launch','command':['unused']}))
        _,f.spec=read_profile(profile)
        f.policy,_=policy_fixture(f.root,'test-profile',f.spec)
        policy=json.loads(f.policy.path.read_text())
        for entries in policy['profiles']['test-profile']['permissions'].values():entries.append(permission)
        write(f.policy.path,policy)
        write(f.root/'tools.json',{'version':1,'tools':{'read':registration}})
        f.approval=secrets.token_urlsafe(32)
        write(f.root/'approvers.json',{'version':1,'approvers':[dict(enabled=True,subject='operator:independent',token_sha256=hashlib.sha256(f.approval.encode()).hexdigest(),scopes=[f.scope],tools=['read'])]})
        f.gate=ToolInvocations(f.store,f.policy,FileToolCatalog(f.root/'tools.json'),f.provider,McpHttpTransport(),FileApprovers(f.root/'approvers.json'))
        f.run=await f.store.submit('identity-tools',dict(runtime_profile='test-profile',profile_digest=f.spec.fingerprint(),secret_refs=[]),authorization=f.policy.admission('test-profile',f.spec.fingerprint(),[]))
        await f.store.register_worker('identity-probe','fixture-instance','synthetic-owner',os.getpid())
        f.attempt=await f.store.claim('identity-probe','fixture-instance')
        f.binding=[f.run['id'],f.attempt['id'],'identity-probe','fixture-instance',f.attempt['epoch']]
        context=WorkAuthorizationContext(f.scope,*f.binding[:2],*f.binding[2:4],f.attempt['epoch'],f.attempt['authorization_epoch'],None,f.run['input']['work_authorization_ref'])
        verifier=WorkAuthorizationVerifier(f.store,f.policy)
        await verifier(context,'execute')
        await f.store.report(*f.binding,1,'preparing');await f.store.report(*f.binding,2,'creating_session')
        await verifier(replace(context,session_id='identity-probe-session'),'execute')
        await f.store.report(*f.binding,3,'waiting_model',session_id='identity-probe-session')
        f.registry=f.root/'registry.json';write(f.registry,{'version':1,'profiles':{'test-profile':{'enabled':True,'path':str(profile)}}})
        return f
    except BaseException:
        if getattr(f,'mcp',None):f.mcp.kill();await f.mcp.communicate()
        f.provider.close();await f.asyncTearDown();raise


async def qualify(args):
    import uvicorn
    output=args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT/'workspace'):raise ValueError('new workspace output required')
    output.mkdir(mode=0o700,parents=True)
    checks=[];observations=[];relays=[];tunnel=None;api_task=None;gateway_task=None;heartbeat=None;tool=None;fixture=None
    stop=asyncio.Event();api=None;protected=[];transcripts=[]
    errors=CaptureErrors();logging.getLogger().addHandler(errors)
    report={'work_item':'K03-1.03','passed':False,'complete':False,'mode':'local-independent-process' if args.local else 'physical-two-host','checks':checks,'observations':observations}
    local_boot=hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()
    probe=ROOT/'scripts/fixtures/identity_access_probe.py'
    source=base64.b64encode(probe.read_bytes()).decode()
    ssh=['ssh','-F',str(args.ssh_config),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2'] if not args.local else []
    async def remote(value):
        command=[sys.executable,str(probe)] if args.local else ssh+[args.host,shlex.join(['python3','-c',"import base64;exec(base64.b64decode('"+source+"'))"])]
        process=await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:out,err=await asyncio.wait_for(process.communicate(json.dumps(value).encode()),25)
        except BaseException:
            if process.returncode is None:process.kill()
            await process.communicate();raise
        transcripts.extend([out,err])
        if process.returncode:raise RuntimeError('probe process failed; inspect private transcript')
        result=json.loads(out)
        assert result['pid']!=os.getpid() or not args.local
        assert (result['boot_sha256']==local_boot)==args.local,'host separation mismatch'
        observations.append(result)
        return result
    def mark(name):checks.append(name);print(name,flush=True)
    try:
        fixture=PrincipalTests();await fixture.asyncSetUp()
        # Real API server uses the existing fixture application and production middleware.
        certs=type('CertificateDirectory',(),{'root':output})()
        await asyncio.to_thread(RemoteRecoveryQualificationTests.create_certificates,certs)
        private_key=(output/'client.key').read_text();protected.extend([private_key,*fixture.tokens.values(),'operator-test'])
        api=uvicorn.Server(uvicorn.Config(fixture.app,host='127.0.0.1',port=0,log_level='error',access_log=False,
            ssl_certfile=str(output/'server.pem'),ssl_keyfile=str(output/'server.key'),ssl_ca_certs=str(output/'ca.pem'),ssl_cert_reqs=ssl.CERT_REQUIRED))
        api_task=asyncio.create_task(api.serve())
        async with asyncio.timeout(10):
            while not api.started:
                if api_task.done():await api_task
                await asyncio.sleep(.05)
        api_relay=WireRelay(api.servers[0].sockets[0].getsockname()[1]);relays.append(api_relay);api_port=await api_relay.open()
        (output/'source.txt').write_text('受控读取：同内容不同权限🙂')
        tool=await prepare_tool(output)
        token=secrets.token_urlsafe(32);protected.extend([token,tool.approval])
        fingerprint=hashlib.sha256(ssl.PEM_cert_to_DER_cert((output/'client.pem').read_text())).hexdigest()
        now=time.time();identity=dict(enabled=True,subject='worker:qualification',worker_id='identity-probe',certificate_sha256=fingerprint,token_sha256=hashlib.sha256(token.encode()).hexdigest(),not_before=now-1,expires_at=now+600,bindings=[{'scope':tool.scope,'profile':'test-profile'}])
        identities=output/'workers.json'
        def save_identity():write(identities,{'version':1,'audience':'qualification','workers':[identity]})
        save_identity()
        gateway=ToolGateway(tool.gate,WorkerIdentities(identities,'qualification'),tool.registry)
        gateway_task=asyncio.create_task(gateway.serve('127.0.0.1',0,tls_context(output/'ca.pem',output/'server.pem',output/'server.key',server=True),stop))
        async with asyncio.timeout(5):
            while gateway.server is None:await asyncio.sleep(.01)
        tool_relay=WireRelay(gateway.server.sockets[0].getsockname()[1]);relays.append(tool_relay);tool_port=await tool_relay.open()
        async def keep_owned():
            while True:
                await tool.store.heartbeat(*tool.binding);await asyncio.sleep(5)
        heartbeat=asyncio.create_task(keep_owned())
        if not args.local:
            tunnel=await asyncio.create_subprocess_exec(*ssh,'-S','none','-o','ExitOnForwardFailure=yes','-N',
                '-R',f'127.0.0.1:{api_port}:127.0.0.1:{api_port}','-R',f'127.0.0.1:{tool_port}:127.0.0.1:{tool_port}',args.host,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            ready=ssh+[args.host,shlex.join(['python3','-c',
                'import socket,time; deadline=time.monotonic()+8\nwhile True:\n try:\n  s=socket.create_connection((\"127.0.0.1\",'+str(api_port)+'),1);s.close();break\n except OSError:\n  if time.monotonic()>deadline:raise\n  time.sleep(.1)'])]
            process=await asyncio.create_subprocess_exec(*ready,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:out,err=await asyncio.wait_for(process.communicate(),20)
            except BaseException:
                if process.returncode is None:process.kill()
                await process.communicate();raise
            assert process.returncode==0,'remote forward readiness failed'
        tls=dict(ca=(output/'ca.pem').read_text(),certificate=(output/'client.pem').read_text(),key=private_key)
        async def get(path,who='a',status=200,**extra):
            value=await remote(dict(tls,mode='http',port=api_port,path='/api/os/runtime/'+path,token=fixture.tokens[who],**extra))
            assert value.get('status')==status,value
            if status==200:assert value['cache_control']=='no-store'
            return value
        def path(ref):return f"objects/{ref['kind']}/{ref['id']}/versions/{ref['version']}"
        async def grant(ref,who,revision=0,key=None):
            response=await fixture.client.put('/api/os/runtime/'+path(ref)+'/policy',headers=fixture.headers,json=dict(grants=[dict(principal_id=p,actions=['object.read','object.download'],expires_at=time.time()+600) for p in who],expected_revision=revision,idempotency_key=key or secrets.token_hex(8)))
            assert response.status_code==200,response.text
        data='相同内容，独立权限🙂'.encode();refs=[]
        for key in ('one','two'):
            response=await fixture.client.post('/api/os/runtime/objects/files',headers=fixture.headers,json=dict(content_base64=base64.b64encode(data).decode(),idempotency_key=key))
            assert response.status_code==200,response.text
            refs.append(response.json())
        assert refs[0]['ref']!=refs[1]['ref'] and refs[0]['descriptor']==refs[1]['descriptor']
        a,b=[r['ref'] for r in refs];await grant(a,['a']);await grant(b,['b'])
        for who,own,denied in [('a',a,b),('b',b,a)]:
            result=await get(path(own)+'/download',who);assert result['sha256']==hashlib.sha256(data).hexdigest()
            await get(path(denied)+'/download',who,404)
        mark('identical-content-distinct-object-ACL-at-actual-HTTPS-read')
        await grant(a,[],1,'revoke-a');await get(path(a)+'/download','a',404)
        fixture.directory['principals']['b']['tokens'][0]['enabled']=False;fixture.save()
        await get(path(b)+'/download','b',401)
        mark('ACL-and-bearer-token-revocation-reject-actual-read')
        fixture.directory['principals']['b']['tokens'][0]['enabled']=True;fixture.directory['principals']['b']['tokens'][0]['expires_at']=time.time()-1;fixture.save()
        await get(path(b)+'/download','b',401)
        result=await remote(dict(tls,mode='http',port=api_port,path='/api/os/runtime/identity',token=fixture.tokens['a'],server_name='wrong.invalid'))
        assert result['error']=='TLS_CERTIFICATE_REJECTED'
        mark('expired-token-and-wrong-server-certificate-rejected')
        def envelope(method,params):return dict(protocol=PROTOCOL,audience='qualification',scope=tool.scope,profile='test-profile',profile_digest=tool.spec.fingerprint(),binding=tool.binding,access_token=token,message={'method':method,'params':params})
        async def rpc(method,params=None,expected=None,changes=None):
            payload=envelope(method,params or {});payload.update(changes or {})
            result=await remote(dict(tls,mode='rpc',port=tool_port,envelope=payload))
            value=result['response']
            if expected:assert value=={'ok':False,'error':expected},value
            else:assert value['ok'],value
            return value.get('value')
        await rpc('describe')
        await rpc('describe',expected='WORKER_IDENTITY_DENIED',changes={'access_token':secrets.token_urlsafe(32)})
        await rpc('describe',expected='WORKER_IDENTITY_SCOPE_DENIED',changes={'audience':'other'})
        identity['certificate_sha256']='f'*64;save_identity()
        await rpc('describe',expected='WORKER_IDENTITY_DENIED')
        identity['certificate_sha256']=fingerprint;identity['expires_at']=time.time()-1;save_identity()
        await rpc('describe',expected='WORKER_IDENTITY_EXPIRED_OR_REVOKED')
        identity['expires_at']=now+600;save_identity()
        mark('remote-tool-certificate-token-expiry-and-audience-bound')
        async def approved(key):
            item=await rpc('request',{'tool':'read','arguments':{},'key':key})
            assert item['state']=='awaiting_approval'
            await tool.gate.decide(tool.run['id'],item['id'],item['version'],'approved',tool.approval)
            return item
        item=await approved('success');result=await rpc('advance',{'invocation_id':item['id']});assert result['state']=='succeeded'
        def calls():
            return [json.loads(v) for v in (output/'events.jsonl').read_text().splitlines() if json.loads(v).get('method')=='tools/call']
        assert len(calls())==1
        pending=await approved('revoked');identity['enabled']=False;save_identity()
        await rpc('advance',{'invocation_id':pending['id']},expected='WORKER_IDENTITY_EXPIRED_OR_REVOKED');assert len(calls())==1
        identity['enabled']=True;save_identity()
        (output/'hold').touch()
        call=asyncio.create_task(rpc('advance',{'invocation_id':pending['id']},expected='WORKER_IDENTITY_EXPIRED_OR_REVOKED'))
        try:
            async with asyncio.timeout(12):
                while not any(json.loads(v).get('barrier')=='initialize' for v in (output/'events.jsonl').read_text().splitlines()):await asyncio.sleep(.02)
            identity['enabled']=False;save_identity();(output/'release').touch();await call
        finally:
            (output/'release').touch()
            if not call.done():call.cancel()
            await asyncio.gather(call,return_exceptions=True)
        assert len(calls())==1
        mark('machine-revocation-before-and-during-MCP-initialization-blocks-actual-tool-call')
        identity['enabled']=True;save_identity();run=await tool.store.get(tool.run['id']);await tool.store.revoke(run['id'],run['version'])
        await rpc('describe',expected='AUTH_REVOKED');assert len(calls())==1
        mark('E2AG-work-revocation-rejects-remote-channel')
        for value in protected:
            raw=value.encode()
            assert all(raw not in relay.data for relay in relays)
            assert all(raw not in transcript for transcript in transcripts)
            for file in tool.provider.root.rglob('*'):
                if file.is_file():assert raw not in file.read_bytes()
        assert all(relay.data for relay in relays)
        mark('captured-TLS-wire-client-output-and-tool-artifacts-have-no-credential-values')
        if heartbeat.done():heartbeat.result()
        assert not errors.messages,errors.messages
        report['server_errors']=[]
        report.update(passed=True,complete=not args.local,implementation_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            controller_boot_sha256=local_boot,wire=[dict(bytes=len(r.data),sha256=hashlib.sha256(r.data).hexdigest()) for r in relays],
            tool_calls=len(calls()),run_id=tool.run['id'],attempt_id=tool.attempt['id'],paid_model_calls=0,
            limits=['Synthetic active Attempt is established by the qualification administrator; no Agent or customer task is launched.','Local mode does not satisfy physical two-host acceptance.','MCP source is synthetic on controller loopback; cross-host traffic carries mTLS or HTTPS.','Existing native IdP/credential rotation evidence remains separately referenced.'])
        report['source_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),probe]}
    finally:
        if heartbeat:heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
        if api:api.should_exit=True
        if api_task:await asyncio.wait_for(api_task,10)
        stop.set()
        if gateway_task:await asyncio.wait_for(gateway_task,15)
        for relay in relays:await relay.close()
        if tunnel and tunnel.returncode is None:tunnel.terminate();await tunnel.communicate()
        if tool:
            if tool.mcp.returncode is None:tool.mcp.terminate()
            _,err=await tool.mcp.communicate();assert all(v.encode() not in err for v in protected)
            tool.provider.close();await tool.asyncTearDown()
        if fixture:fixture.console.provider.close();await fixture.asyncTearDown()
        logging.getLogger().removeHandler(errors)
        if errors.messages:
            report.update(passed=False,complete=False,server_errors=errors.messages)
        (output/'probe-transcripts.log').write_bytes(b'\n'.join(transcripts))
        # Public report contains no credentials; private fixture files are owner-only.
        for file in output.iterdir():
            if file.is_file():file.chmod(0o600)
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--local',action='store_true');p.add_argument('--ssh-config',type=Path);p.add_argument('--host')
    args=p.parse_args()
    if not args.local and not (args.ssh_config and args.host):p.error('--ssh-config and --host required for physical mode')
    value=asyncio.run(qualify(args));print(json.dumps({k:value[k] for k in ('passed','complete','mode','checks')}))
    return 0 if value['passed'] else 1


if __name__=='__main__':raise SystemExit(main())