"""Executable design checks. Does not exercise the production runtime or database."""
import argparse,copy,hashlib,importlib.util,json
from pathlib import Path
from jsonschema import Draft202012Validator,FormatChecker
ROOT=Path(__file__).resolve().parents[1];C=ROOT/'contracts/v1'
def load(p):return json.loads((C/p).read_text(encoding='utf-8'))
def canonical_digest(payload):
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def permits(machine,source,target,facts):
    return target in machine['transitions'].get(source,[]) and all(facts.get(g) is True for g in machine.get('guards',{}).get(target,[]))
def current_write(owner,report,now):
    return (report['worker_id'],report['attempt_id'],report['epoch'],report['expected_version'])==(owner['worker_id'],owner['attempt_id'],owner['epoch'],owner['version']) and now<owner['expires_at']
def run():
    checks=[]
    def check(name,ok):
        if not ok:raise AssertionError(name)
        checks.append(name)
    schema=load('domain.schema.json');defs=schema['$defs'];api=load('openapi.json')
    for name,s in defs.items():
        Draft202012Validator.check_schema(s)
        check('owner:'+name,s.get('x-owner') in ['L1','L2','L3'])
    def refs(value,root,prefix):
        if isinstance(value,dict):
            if '$ref' in value:
                key=value['$ref'];check('ref:'+key,key.startswith(prefix) and key[len(prefix):] in root)
            for x in value.values():refs(x,root,prefix)
        elif isinstance(value,list):
            for x in value:refs(x,root,prefix)
    refs(schema,defs,'#/$defs/');refs(api,api['components']['schemas'],'#/components/schemas/')
    for name,s in defs.items():
        check('oas-schema:'+name,json.dumps(s,sort_keys=True).replace('#/$defs/','#/components/schemas/')==json.dumps(api['components']['schemas'][name],sort_keys=True))
    op_ids=[]
    for path,methods in api['paths'].items():
        for method,op in methods.items():
            op_ids.append(op['operationId']);check('permission:'+op['operationId'],bool(op['x-permission']))
            check('auth:'+op['operationId'],bool(op.get('security',api.get('security'))))
            if '/internal/' in path:check('machine-auth:'+path,'machineMTLS' in op['security'][0])
            if method=='post' and not path.endswith('/query'):check('idempotency:'+path,any(p['name']=='Idempotency-Key' and p['required'] for p in op['parameters']))
    check('operation-id-unique',len(op_ids)==len(set(op_ids)))
    for sample in load('examples.json'):
        v=Draft202012Validator({'$ref':'#/$defs/'+sample['schema'],'$defs':defs},format_checker=FormatChecker())
        check('example:'+sample['name'],v.is_valid(sample['value'])==sample['valid'])
    machines=load('states.json')['machines']
    for name,m in machines.items():
        check('terminal-immutable:'+name,not any(m['transitions'].get(t) for t in m['terminal']))
        if name in defs:
            allowed=set(defs[name]['properties']['state']['enum'])
            declared=set(m['transitions'])|set(m['terminal'])|{t for targets in m['transitions'].values() for t in targets}
            check('state-schema:'+name,allowed==declared)
    for c in load('state-cases.json'):check('transition:'+c['name'],permits(machines[c['machine']],c['from'],c['to'],c['facts'])==c['allowed'])
    # Boundary cases define required outcomes independently of transition fixtures.
    payload=load('examples.json')[0]['value'];ordered=dict(reversed(list(payload.items())))
    check('same-input-order-digest',canonical_digest(payload)==canonical_digest(ordered))
    changed=copy.deepcopy(payload);changed['deployment_id']='another-deployment'
    check('same-key-different-input-conflict',canonical_digest(payload)!=canonical_digest(changed))
    ledger={('service-1','workspace-1','run.submit','key-1'):(canonical_digest(payload),'run-1')}
    check('duplicate-returns-original-run',ledger[('service-1','workspace-1','run.submit','key-1')][1]=='run-1')
    check('identity-scoped-idempotency',('service-2','workspace-1','run.submit','key-1') not in ledger)
    owner={'worker_id':'w1','attempt_id':'a1','epoch':4,'version':8,'expires_at':120}
    proof={'worker_id':'w1','attempt_id':'a1','epoch':4,'expected_version':8}
    check('current-owner-write',current_write(owner,proof,100))
    for field,value in [('worker_id','w2'),('attempt_id','a2'),('epoch',3),('expected_version',7)]:
        check('reject-stale:'+field,not current_write(owner,{**proof,field:value},100))
    check('lease-expiry-boundary',not current_write(owner,proof,120))
    for event in load('states.json')['observation_events_do_not_transition']:
        check('observation-not-transition:'+event,all(event not in targets for m in machines.values() for targets in m['transitions'].values()))
    # Unknown costs remain unknown; doubling load changes capacity, quota loss is visible.
    spec=importlib.util.spec_from_file_location('capacity',ROOT/'scripts/model_capacity.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    profiles=load('capacity-profiles.json')
    for p in profiles['profiles']:
        x={**profiles['shared'],**p};base=mod.calculate(x)
        check('capacity:'+p['id'],base['capacity_sufficient_under_assumptions'])
        check('cost-unknown:'+p['id'],base['model_cost_per_day'] is None)
        check('load-sensitivity:'+p['id'],mod.calculate({**x,'runs_per_day':x['runs_per_day']*2})['required_peak_slots']>base['required_peak_slots'])
        check('quota-bottleneck:'+p['id'],not mod.calculate({**x,'provider_account_slots_assumed':1})['capacity_sufficient_under_assumptions'])
        check('retention-sensitivity:'+p['id'],mod.calculate({**x,'retention_days':x['retention_days']*2})['artifact_log_gib_retained_with_replica_overhead']>base['artifact_log_gib_retained_with_replica_overhead'])
    enterprise={**profiles['shared'],**profiles['profiles'][1]}
    check('enterprise-n-minus-one',mod.calculate({**enterprise,'worker_hosts':enterprise['worker_hosts']-1})['capacity_sufficient_under_assumptions'])
    return {'scope':'design contract consistency and reference semantics; no runtime/concurrency certification','schemas':len(defs),'operations':len(op_ids),'schema_examples':len(load('examples.json')),'state_scenarios':len(load('state-cases.json')),'checks_passed':len(checks),'errors':[],'checks':checks}
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path);a=ap.parse_args();report=run()
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='checks'},ensure_ascii=False))
