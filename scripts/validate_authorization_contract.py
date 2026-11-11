"""Authorization design oracle and fixtures; does not implement cryptography or a PEP."""
import argparse,copy,hashlib,json
from pathlib import Path
from jsonschema import Draft202012Validator,FormatChecker
ROOT=Path(__file__).resolve().parents[1];C=ROOT/'contracts/authz/v1'
def load(name):return json.loads((C/name).read_text(encoding='utf-8'))
def digest(x):return 'sha256:'+hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def result(verdict,reason):return {'verdict':verdict,'reason':reason}
def decide(x,profile):
    schemas=load('authorization.schema.json')['$defs']
    if not Draft202012Validator({'$ref':'#/$defs/DecisionContext','$defs':schemas}).is_valid(x):return result('deny','CONTEXT_INVALID')
    predicates=[(x['authenticated'],'AUTH_INVALID'),(x['credential_context_valid'],'CREDENTIAL_CONTEXT_INVALID'),(x['sender_bound'],'SENDER_MISMATCH'),(x['actor_ref']==x['registered_actor_ref'],'ACTOR_MISMATCH'),(x['binding']==x['expected_binding'],'BINDING_CHANGED'),(x['now']<x['lease_expires_at'],'STALE_OWNER'),(x['grant_status']=='active','GRANT_INACTIVE'),(x['now']<x['grant_expires_at'],'GRANT_EXPIRED'),(x['now']<x['workload_expires_at'],'WORKLOAD_EXPIRED'),(x['binding']['policy_version']==x['policy_version'],'POLICY_CHANGED'),(x['authorization_epoch']==x['current_authorization_epoch'],'AUTH_REVOKED'),(0<=x['now']-x['revocation_checked_at']<=profile['control_cache_max_seconds'],'AUTH_STATE_STALE'),(x['authority_available'],'AUTHORITY_UNAVAILABLE'),(x['audit_available'],'AUDIT_UNAVAILABLE'),(x['admission_governance_complete'],'GOVERNANCE_MISSING'),(x['control_evidence']=='tested','CONTROL_UNVERIFIED'),(x['control_point']==x['required_control_point'],'CONTROL_MISMATCH'),(x['resource_verified'],'RESOURCE_UNVERIFIED'),(x['parameters_validated'],'PARAMETERS_INVALID'),(digest(x['parameters'])==x['parameter_digest'],'PARAMETER_DIGEST_MISMATCH')]
    for ok,reason in predicates:
        if not ok:return result('deny',reason)
    if x['readonly_profile'] and x['action'] in profile['readonly_forbidden_actions']:return result('deny','READ_ONLY_REQUIRED')
    wanted={k:x[k] for k in ['action','resource_kind','resource_id','resource_version','constraint_revision']}
    scopes=[x['scope_layers'][k] for k in profile['required_scope_layers']]+[x['grant_permissions']]
    if not all(wanted in permissions for permissions in scopes):return result('deny','SCOPE_DENIED')
    if not x['budget_available']:return result('deny','BUDGET_EXHAUSTED')
    if x['action'] in profile['approval_actions']:
        a=x['approval']
        if a is None:return result('approval_required','APPROVAL_REQUIRED')
        if a['status']=='consumed':return result('deny','APPROVAL_REPLAY')
        if x['now']>=a['expires_at']:return result('deny','APPROVAL_EXPIRED')
        if a['status']!='approved':return result('deny','APPROVAL_NOT_APPROVED')
        if not (a['approver_authorized'] and a['separation_of_duties']):return result('deny','APPROVER_INVALID')
        if any(a[k]!=x[k] for k in ['binding','action','resource_kind','resource_id','resource_version','parameter_digest','policy_version','authorization_epoch']) or a['permission_digest']!=digest(x['grant_permissions']):return result('deny','APPROVAL_BINDING_CHANGED')
    return result('allow','AUTHORIZED')
def renewal(grant,current,submitted_digest,profile):
    if not current['authority_available'] or not current['audit_available']:return {'reason':'AUTHORITY_UNAVAILABLE'}
    if current['expected_version']!=grant['version']:return {'reason':'VERSION_CONFLICT'}
    if current['workload_status']!='active':return {'reason':'WORKLOAD_INACTIVE'}
    if set(current['scope_layers'])!=set(profile['required_scope_layers']):return {'reason':'CONTEXT_INVALID'}
    if grant['status']!='active' or current['now']>=grant['expires_at']:return {'reason':'GRANT_INACTIVE'}
    if grant['binding']!=current['binding']:return {'reason':'BINDING_CHANGED'}
    if grant['authorization_epoch']!=current['authorization_epoch']:return {'reason':'AUTH_REVOKED'}
    if submitted_digest!=digest(grant['permissions']):return {'reason':'SCOPE_CHANGED'}
    if current['now']>=current['lease_expires_at'] or current['now']>=current['workload_expires_at']:return {'reason':'OWNER_OR_WORKLOAD_EXPIRED'}
    effective=[p for p in grant['permissions'] if all(p in layer for layer in current['scope_layers'].values())]
    if not effective:return {'reason':'SCOPE_EMPTY'}
    return {'reason':'RENEWED','permissions':effective,'expires_at':min(current['now']+profile['execution_grant_ttl_seconds'],current['workload_expires_at']),'version':grant['version']+1}
def consume(approval,invocation_id,parameter_digest):
    # Reference transition only: production requires a transaction and unique constraints.
    if approval['parameter_digest']!=parameter_digest:return 'BINDING_CHANGED'
    if approval['status']=='consumed':return 'EXISTING_INTENT' if approval['invocation_id']==invocation_id else 'APPROVAL_REPLAY'
    if approval['status']!='approved':return 'APPROVAL_NOT_APPROVED'
    approval.update(status='consumed',invocation_id=invocation_id);return 'NEW_INTENT'
def run():
    checks=[]
    def check(name,ok):
        if not ok:raise AssertionError(name)
        checks.append(name)
    root=load('authorization.schema.json');defs=root['$defs'];api=load('openapi.json');core=json.loads((ROOT/'contracts/v1/openapi.json').read_text())
    original=json.loads((ROOT/'contracts/v1/domain.schema.json').read_text())['$defs']
    for name in ['ExecutionBinding','ResourceRef','Error']:check('shared-schema:'+name,defs[name]==original[name])
    for name,schema in defs.items():Draft202012Validator.check_schema(schema);check('owner:'+name,schema['x-owner']=='L1')
    def check_refs(x,names,prefix):
        if isinstance(x,dict):
            if '$ref' in x:check('reference:'+x['$ref'],x['$ref'].startswith(prefix) and x['$ref'][len(prefix):] in names)
            for v in x.values():check_refs(v,names,prefix)
        if isinstance(x,list):
            for v in x:check_refs(v,names,prefix)
    check_refs(root,defs,'#/$defs/');check_refs(api,defs,'#/components/schemas/')
    for n,v in defs.items():check('oas-schema:'+n,json.loads(json.dumps(v).replace('#/$defs/','#/components/schemas/'))==api['components']['schemas'][n])
    for sample in load('examples.json'):
        v=Draft202012Validator({'$ref':'#/$defs/'+sample['schema'],'$defs':defs},format_checker=FormatChecker())
        check('example:'+sample['name'],v.is_valid(sample['value'])==sample['valid'])
    matrix=load('permission-matrix.json')['operations'];by_id={x['operation_id']:x for x in matrix};op_count=0
    check('unique-permission-mapping',len(by_id)==len(matrix))
    for spec in [core,api]:
        for path,methods in spec['paths'].items():
            for method,op in methods.items():
                op_count+=1;m=by_id[op['operationId']]
                check('permission:'+op['operationId'],(m['path'],m['method'],m['action'])==(path,method.upper(),op['x-permission']) and m['deny_on_missing_scope'])
                check('profile:'+op['operationId'],op['x-authorization-profile']=='dios.authz.managed.v1')
                if '/internal/' in path:check('machine-auth:'+op['operationId'],'machineMTLS' in op['security'][0])
    check('full-operation-coverage',op_count==len(matrix))
    profile=load('profile.json');fixtures=load('decision-cases.json');base=fixtures['base']
    for case in fixtures['cases']:
        x=copy.deepcopy(base)
        for path,value in case['overrides'].items():
            parts=path.split('.');node=x
            for key in parts[:-1]:node=node[key]
            node[parts[-1]]=value
        check('decision:'+case['name'],decide(x,profile)==case['expected'])
    grant={'status':'active','binding':base['binding'],'authorization_epoch':5,'expires_at':1200,'permissions':base['grant_permissions'],'version':1}
    current={'binding':base['binding'],'authorization_epoch':5,'now':1100,'lease_expires_at':1150,'workload_expires_at':1500,'scope_layers':base['scope_layers'],'authority_available':True,'audit_available':True,'expected_version':1,'workload_status':'active'}
    renewed=renewal(grant,current,digest(grant['permissions']),profile)
    check('renew-parent-deadline',renewed['reason']=='RENEWED' and renewed['expires_at']==1500)
    check('renew-authority-outage',renewal(grant,{**current,'authority_available':False},digest(grant['permissions']),profile)['reason']=='AUTHORITY_UNAVAILABLE')
    check('renew-version-race',renewal(grant,{**current,'expected_version':0},digest(grant['permissions']),profile)['reason']=='VERSION_CONFLICT')
    check('renew-empty-scope-context',renewal(grant,{**current,'scope_layers':{}},digest(grant['permissions']),profile)['reason']=='CONTEXT_INVALID')
    check('renew-workload-revoked',renewal(grant,{**current,'workload_status':'revoked'},digest(grant['permissions']),profile)['reason']=='WORKLOAD_INACTIVE')
    check('renew-cannot-change-requested-scope',renewal(grant,current,'sha256:'+'0'*64,profile)['reason']=='SCOPE_CHANGED')
    check('revoked-renewal',renewal({**grant,'status':'revoked'},current,digest(grant['permissions']),profile)['reason']=='GRANT_INACTIVE')
    check('new-owner-renewal',renewal(grant,{**current,'binding':{**current['binding'],'lease_epoch':8}},digest(grant['permissions']),profile)['reason']=='BINDING_CHANGED')
    check('revoked-subject-renewal',renewal(grant,{**current,'authorization_epoch':6},digest(grant['permissions']),profile)['reason']=='AUTH_REVOKED')
    reduced=copy.deepcopy(current);reduced['scope_layers']['caller']=grant['permissions'][:1]
    check('renew-shrinks-scope',renewal(grant,reduced,digest(grant['permissions']),profile)['permissions']==grant['permissions'][:1])
    a={'status':'approved','parameter_digest':base['parameter_digest']}
    check('approval-first-consume',consume(a,'inv-1',base['parameter_digest'])=='NEW_INTENT')
    check('approval-same-invocation-replay',consume(a,'inv-1',base['parameter_digest'])=='EXISTING_INTENT')
    check('approval-new-invocation-denied',consume(a,'inv-2',base['parameter_digest'])=='APPROVAL_REPLAY')
    # Removing any required scope can never turn a denied request into allow.
    for layer in profile['required_scope_layers']:
        x=copy.deepcopy(base);x['scope_layers'][layer]=[]
        check('scope-monotonic:'+layer,decide(x,profile)['verdict']=='deny')
    return {'scope':'authorization design/reference semantics; no cryptographic, sandbox, database concurrency or third-party Agent certification','schemas':len(defs),'mapped_operations':op_count,'decision_scenarios':len(fixtures['cases']),'checks_passed':len(checks),'errors':[],'checks':checks}
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path);a=ap.parse_args();report=run()
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='checks'},ensure_ascii=False))
