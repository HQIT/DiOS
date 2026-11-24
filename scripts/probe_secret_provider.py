"""Qualify native OpenBao KV v2 and Agent against synthetic secrets only."""
import argparse,json,os,secrets,subprocess,tempfile,time,sys
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.runtime.openbao_secrets import OpenBaoSecrets
from app.runtime.database import RuntimeErrorCode
IMAGE='quay.io/openbao/openbao:2.7.3@sha256:ba4b133eb9586630024e71a617e66f37f29cc3534cdd46a6233e098ceded8dc9'

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--port',type=int,default=13189);args=p.parse_args()
 base=f'http://127.0.0.1:{args.port}';checks=[];containers=[]
 def docker(*args):return subprocess.check_output(['docker',*args],text=True,stderr=subprocess.PIPE).strip()
 def private(path,value):path.write_text(value);path.chmod(0o600)
 def mark(name):checks.append(name);print(name,flush=True)
 with tempfile.TemporaryDirectory(prefix='dios-bao-') as temp,httpx.Client(base_url=base,trust_env=False,timeout=2) as http:
  root=Path(temp);(root/'data').mkdir();(root/'agent').mkdir();admin={}
  def req(method,path,body=None,token=None):return http.request(method,'/v1/'+path,json=body,headers={'X-Vault-Token':token or admin.get('token','')},timeout=30 if path in ('sys/init','sys/unseal') else 2)
  def ok(method,path,body=None,token=None):
   response=req(method,path,body,token);assert response.is_success,(method,path,response.status_code,response.json().get('errors') if path in ('sys/mounts/secret','secret/data/source') else 'provider error');return response.json() if response.content else {}
  def wait_for(fn,seconds=40):
   end=time.monotonic()+seconds
   while time.monotonic()<end:
    try:
     value=fn()
     if value:return value
    except (httpx.HTTPError,FileNotFoundError):pass
    time.sleep(.2)
   raise AssertionError('Timed out waiting for native provider')
  private(root/'server.json',json.dumps({'storage':{'raft':{'path':'/fixture/data','node_id':'qualification'}},'listener':{'tcp':{'address':f'127.0.0.1:{args.port}','tls_disable':True}},'api_addr':base,'cluster_addr':f'http://127.0.0.1:{args.port+1}'}))
  try:
   server=docker('run','-d','--network','host','--user',f'{os.getuid()}:{os.getgid()}','--memory','512m','--label','dios.purpose=secret-qualification','-v',str(root)+':/fixture',IMAGE,'server','-config=/fixture/server.json');containers.append(server)
   wait_for(lambda:http.get('/v1/sys/health').status_code==501)
   init=ok('POST','sys/init',{'secret_shares':1,'secret_threshold':1});admin['token']=init['root_token'];unseal=init['keys_base64'][0]
   ok('POST','sys/unseal',{'key':unseal});wait_for(lambda:http.get('/v1/sys/health').status_code==200);mark('normal-server-init-unseal')
   ok('POST','sys/mounts/secret',{'type':'kv','options':{'version':'2'}});wait_for(lambda:req('GET','secret/config').status_code==200)
   first='first-'+secrets.token_hex(16);second='second-'+secrets.token_hex(16)
   assert ok('POST','secret/data/source',{'options':{'cas':0},'data':{'token':first}})['data']['version']==1
   assert req('POST','secret/data/source',{'options':{'cas':0},'data':{'token':'stale'}}).status_code==400;mark('kv-create-cas-rejects-stale')
   assert ok('POST','secret/data/source',{'options':{'cas':1},'data':{'token':second}})['data']['version']==2
   assert ok('GET','secret/data/source?version=1')['data']['data']['token']==first
   assert ok('GET','secret/data/source?version=2')['data']['data']['token']==second;mark('immutable-version-selection')
   ok('POST','secret/data/other',{'data':{'token':'not-authorized'}})
   ok('PUT','sys/policies/acl/reader',{'policy':'path "secret/data/source" { capabilities = ["read"] }'})
   ok('POST','sys/auth/approle',{'type':'approle'})
   ok('POST','auth/approle/role/reader',{'token_policies':['reader'],'token_ttl':'8s','token_max_ttl':'20s','token_num_uses':0,'secret_id_ttl':'5m'})
   role=ok('GET','auth/approle/role/reader/role-id')['data']['role_id'];secret=ok('POST','auth/approle/role/reader/secret-id')['data']['secret_id']
   private(root/'agent/role',role);private(root/'agent/secret',secret)
   config=f'vault {{ address = "{base}" }}\n' + '\n'.join([
    'auto_auth {','  method "approle" {','    config = {',
    '      role_id_file_path = "/fixture/agent/role"','      secret_id_file_path = "/fixture/agent/secret"','    }','  }',
    '  sink "file" {','    config = {','      path = "/fixture/agent/token"','      mode = 384','    }','  }','}'])
   private(root/'agent.hcl',config)
   agent=docker('run','-d','--network','host','--user',f'{os.getuid()}:{os.getgid()}','--memory','256m','--label','dios.purpose=secret-qualification','-v',str(root)+':/fixture',IMAGE,'agent','-config=/fixture/agent.hcl');containers.append(agent)
   sink=root/'agent/token';token=wait_for(lambda:sink.read_text().strip());assert sink.stat().st_mode&0o777==0o600;assert not (root/'agent/secret').exists();mark('native-agent-approle-private-sink-consumed-secret-id')
   assert ok('GET','secret/data/source?version=1',token=token)['data']['data']['token']==first
   for method,path,body in [('GET','secret/data/other',None),('POST','secret/data/source',{'data':{'token':'forbidden'}}),('DELETE','secret/data/source',None)]:assert req(method,path,body,token).status_code==403
   mark('reader-policy-denies-other-write-delete')
   reader=OpenBaoSecrets({'address':base,'token_file':str(sink),'timeout_seconds':2});reference={'mount':'secret','path':'source','version':1,'field':'token'}
   assert reader.read(reference)==first;mark('runtime-adapter-reads-pinned-version')
   time.sleep(10)
   assert req('GET','secret/data/source?version=1',token=token).status_code==200;mark('native-agent-renews-past-original-ttl')
   fresh=wait_for(lambda:(value if (value:=sink.read_text().strip())!=token else None),25)
   assert req('GET','secret/data/source?version=1',token=fresh).status_code==200
   wait_for(lambda:req('GET','secret/data/source?version=1',token=token).status_code==403,15);mark('native-agent-reauthenticates-at-max-ttl')
   ok('POST','auth/token/revoke',{'token':fresh})
   newer=wait_for(lambda:(value if (value:=sink.read_text().strip())!=fresh else None),25)
   assert req('GET','secret/data/source?version=1',token=newer).status_code==200;mark('native-agent-recovers-revoked-service-token')
   assert reader.read(reference)==first;mark('runtime-adapter-rereads-native-agent-token')
   ok('POST','secret/delete/source',{'versions':[1]});assert req('GET','secret/data/source?version=1',token=newer).status_code==404
   try:reader.read(reference);raise AssertionError('Deleted source accepted')
   except RuntimeErrorCode as exc:assert exc.code=='SOURCE_SECRET_UNAVAILABLE'
   mark('runtime-adapter-rejects-deleted-version')
   ok('POST','secret/undelete/source',{'versions':[1]});assert ok('GET','secret/data/source?version=1',token=newer)['data']['data']['token']==first;mark('version-delete-undelete-semantics')
   ok('PUT','secret/destroy/source',{'versions':[2]});assert req('GET','secret/data/source?version=2',token=newer).status_code==404;mark('destroyed-version-unreadable')
   docker('stop',server)
   try:http.get('/v1/sys/health');raise AssertionError('Stopped provider reachable')
   except httpx.HTTPError:pass
   docker('start',server);wait_for(lambda:http.get('/v1/sys/health').status_code==503);ok('POST','sys/unseal',{'key':unseal});wait_for(lambda:http.get('/v1/sys/health').status_code==200)
   assert ok('GET','secret/data/source?version=1')['data']['data']['token']==first;mark('persistent-restart-sealed-unseal-retains-version')
   evidence={'provider':'OpenBao 2.7.3','image':IMAGE,'checks':checks,'checks_passed':len(checks),'storage':'single-node Raft, initialized/sealed; synthetic secrets only','agent':'native AppRole auto-auth with renewal and reauthentication','remaining':['Enterprise TLS and managed unseal','Physical multihost and HA','Restart bootstrap credential reprovisioning','End-to-end OAuth source and Worker qualification']}
   Path(args.output).write_text(json.dumps(evidence,indent=2)+'\n')
  except BaseException:
   for index,cid in enumerate(containers):
    logs=subprocess.run(['docker','logs',cid],capture_output=True,text=True)
    private(Path(args.output+'.diagnostic-'+str(index)),logs.stdout+logs.stderr)
   raise
  finally:
   for cid in reversed(containers):
    subprocess.run(['docker','rm','-f',cid],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if __name__=='__main__':main()
