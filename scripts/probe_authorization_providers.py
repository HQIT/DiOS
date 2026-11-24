"""Isolated upstream API qualification. Requires three pinned Docker images.

Uses synthetic memory-only stores. Does not connect to a DiOS database, reuse
running providers, benchmark performance, or claim invocation atomicity.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4

LAYERS=('caller','service','workload','resource')
IMAGES={'opa':'openpolicyagent/opa:1.17.0','openfga':'openfga/openfga:v1.23.0','spicedb':'authzed/spicedb:v1.57.1'}
PERMISSION=dict(action='repository.search',resource_kind='mcp_tool',resource_id='review/search',
    resource_version='fixture-v1',constraint_revision='fixture-policy-v1')

def key(permission):return hashlib.sha256(json.dumps(permission,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def docker(*args):return subprocess.check_output(['docker',*args],text=True,stderr=subprocess.PIPE).strip()

class Endpoint:
    def __init__(self,name):
        self.name=name;self.token=secrets.token_urlsafe(32);self.cid=None;self.timings=[];self.revision=None
    def start(self):
        if self.name=='openfga':
            port=8080;args=['run','--datastore-engine=memory','--playground-enabled=false','--authn-method=preshared',
                '--authn-preshared-keys='+self.token,'--log-level=error','--check-query-cache-enabled','--check-query-cache-ttl=1m']
        elif self.name=='spicedb':
            port=8443;args=['serve','--datastore-engine=memory','--grpc-preshared-key='+self.token,
                '--http-enabled','--http-addr=:8443','--telemetry-endpoint=','--skip-release-check','--log-level=error']
        else:
            port=8181;args=['run','--server','--addr=0.0.0.0:8181','--log-level=error','--disable-telemetry']
        self.cid=docker('run','-d','--name','dios-provider-probe-'+self.name+'-'+uuid4().hex[:10],
            '--label','dios.purpose=authorization-qualification','--memory=512m','--cpus=1',
            '-p',f'127.0.0.1::{port}',IMAGES[self.name],*args)
        info=json.loads(docker('inspect',self.cid))[0]
        bound=info['NetworkSettings']['Ports'][str(port)+'/tcp'][0]['HostPort']
        self.base='http://127.0.0.1:'+bound
        self.image=json.loads(docker('image','inspect',IMAGES[self.name]))[0]
        self.identity={'image':IMAGES[self.name],'image_id':self.image['Id'],'repo_digests':self.image['RepoDigests']}
        for _ in range(100):
            try:
                request=urllib.request.Request(self.base+'/',headers={'Authorization':'Bearer '+self.token})
                try:urllib.request.urlopen(request,timeout=.3).close()
                except urllib.error.HTTPError:pass
                return
            except (urllib.error.URLError,TimeoutError):time.sleep(.1)
        raise RuntimeError(self.name+' did not become reachable')
    def request(self,path,body=None,method='POST',raw=False):
        data=body.encode() if raw else (json.dumps(body).encode() if body is not None else None)
        req=urllib.request.Request(self.base+path,data=data,method=method,headers={
            'Content-Type':'text/plain' if raw else 'application/json','Authorization':'Bearer '+self.token})
        start=time.perf_counter()
        try:
            with urllib.request.urlopen(req,timeout=5) as response:
                value=response.read();return json.loads(value) if value else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(self.name+' '+path+' '+str(exc.code)+' '+exc.read().decode()[:500]) from None
        finally:self.timings.append((time.perf_counter()-start)*1000)
    def setup(self):
        if self.name=='openfga':
            self.store=self.request('/stores',{'name':'dios-isolated-qualification'})['id']
            model={'schema_version':'1.1','type_definitions':[{'type':'principal'}, {'type':'permission',
                'relations':{**{x:{'this':{}} for x in LAYERS},'execute':{'intersection':{'child':[
                    {'computedUserset':{'relation':x}} for x in LAYERS]}}},
                'metadata':{'relations':{x:{'directly_related_user_types':[{'type':'principal'}]} for x in LAYERS}}}]}
            self.model=self.request('/stores/'+self.store+'/authorization-models',model)['authorization_model_id']
        elif self.name=='spicedb':
            schema='definition principal {}\ndefinition capability {\n'+''.join('relation '+x+': principal\n' for x in LAYERS)
            schema+='permission execute = '+ ' & '.join(LAYERS)+'\n}'
            self.request('/v1/schema/write',{'schema':schema})
        else:
            rego='package dios.probe\nimport rego.v1\ndefault allow := false\nallow if {\n every layer in ["caller", "service", "workload", "resource"] {\n input.principal in data.grants[input.permission][layer]\n }\n}\n'
            self.request('/v1/policies/dios-probe',rego,'PUT',raw=True);self.data={}
    def change(self,principal,permission,layer,enabled):
        permission=key(permission)
        if self.name=='openfga':
            row={'user':'principal:'+principal,'relation':layer,'object':'permission:'+permission}
            self.request('/stores/'+self.store+'/write',{'authorization_model_id':self.model,
                'writes' if enabled else 'deletes':{'tuple_keys':[row]}})
        elif self.name=='spicedb':
            row={'resource':{'objectType':'capability','objectId':permission},'relation':layer,
                'subject':{'object':{'objectType':'principal','objectId':principal}}}
            response=self.request('/v1/relationships/write',{'updates':[{'operation':'OPERATION_TOUCH' if enabled else 'OPERATION_DELETE','relationship':row}]})
            self.revision=response['writtenAt']
        else:
            row=self.data.setdefault(permission,{x:[] for x in LAYERS})[layer]
            if enabled and principal not in row:row.append(principal)
            elif not enabled and principal in row:row.remove(principal)
            self.request('/v1/data/grants',self.data,'PUT')
    def check(self,principal='a',permission=PERMISSION,consistency=True):
        permission=key(permission)
        if self.name=='openfga':
            response=self.request('/stores/'+self.store+'/check',{'authorization_model_id':self.model,
                'tuple_key':{'user':'principal:'+principal,'relation':'execute','object':'permission:'+permission},
                'consistency':'HIGHER_CONSISTENCY' if consistency else 'MINIMIZE_LATENCY'})
            return response['allowed']
        if self.name=='spicedb':
            response=self.request('/v1/permissions/check',{'resource':{'objectType':'capability','objectId':permission},
                'permission':'execute','subject':{'object':{'objectType':'principal','objectId':principal}},
                'consistency':{'atLeastAsFresh':self.revision} if consistency and self.revision else {'minimizeLatency':True}})
            return response['permissionship']=='PERMISSIONSHIP_HAS_PERMISSION'
        return self.request('/v1/data/dios/probe/allow',{'input':{'principal':principal,'permission':permission}}).get('result') is True
    def stop(self):
        if self.cid:
            cid=self.cid;docker('rm','-f',cid);self.cid=None

def probe(name):
    endpoint=Endpoint(name);checks=[];observations={}
    def expect(label,actual,wanted):
        if actual is not wanted:raise AssertionError((name,label,actual,wanted))
        checks.append({'scenario':label,'expected':wanted,'actual':actual})
    try:
        endpoint.start();endpoint.setup()
        for principal in ('a','b'):
            for layer in LAYERS:endpoint.change(principal,PERMISSION,layer,True)
        expect('complete-tuple-four-layer-allow',endpoint.check(),True)
        for field in PERMISSION:
            changed=dict(PERMISSION);changed[field]+='-different'
            expect('changed-'+field,endpoint.check(permission=changed),False)
        expect('unknown-principal',endpoint.check('unknown'),False)
        for layer in LAYERS:
            endpoint.change('a',PERMISSION,layer,False)
            expect('missing-'+layer+'-denied',endpoint.check(),False)
            endpoint.change('a',PERMISSION,layer,True)
        endpoint.change('b',PERMISSION,'caller',False)
        expect('unrelated-revocation-preserves-a',endpoint.check(),True)
        expect('related-revocation-denies-b',endpoint.check('b'),False)
        # Returned decisions are values, not execution reservations.
        before=endpoint.check();endpoint.change('a',PERMISSION,'caller',False)
        expect('waiting-then-recheck-denies',endpoint.check(),False)
        observations['old_allow_survives_in_caller_memory']=before
        observations['old_allow_is_not_a_dispatch_reservation']=True
        endpoint.change('a',PERMISSION,'caller',True)
        expect('fresh-authorization-after-restore',endpoint.check(),True)
        expect('repeated-read',endpoint.check(),True)
        if name=='spicedb':observations['revocation_checks_use_latest_write_zedtoken']=bool(endpoint.revision)
        if name=='openfga':observations['pinned_model_and_higher_consistency']=True
        identity=endpoint.identity;endpoint.stop()
        try:endpoint.check()
        except (urllib.error.URLError,TimeoutError,ConnectionError):checks.append({'scenario':'provider-unavailable','actual':'transport-error-not-allow'})
        else:raise AssertionError('Stopped provider unexpectedly authorized')
        return {'provider':name,'identity':identity,'checks':checks,'observations':observations,
            'not_tested':['durable-restart','production-scale','cross-database-atomicity','external-effects','DiOS-adapter-integration'],
            'cleanup':'owned-container-removed'}
    finally:endpoint.stop()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output={'scope':'upstream API semantics on synthetic memory stores; not a performance benchmark',
        'permission':PERMISSION,'providers':[]}
    for name in IMAGES:
        result=probe(name);output['providers'].append(result)
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
        print(name+': '+str(len(result['checks']))+' checks passed',flush=True)
    return 0
if __name__=='__main__':raise SystemExit(main())
