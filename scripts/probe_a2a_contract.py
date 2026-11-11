"""Isolated handler survey against A2A 0.3.0; no application/database startup.

Runs selected, unchanged source function bodies using in-memory dependency doubles.
This is a bounded contract survey, not HTTP/auth/runtime conformance certification.
"""
import argparse
import ast
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / 'backend/app/services/a2a_service.py'
ROUTE = ROOT / 'backend/app/api/os/a2a.py'

def load_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                arg.annotation = None
            node.returns = None
            node.args.defaults = [ast.Constant(value=None) if isinstance(value, ast.Call) else value for value in node.args.defaults]
            selected.append(node)
    assert {n.name for n in selected} == set(names)
    exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),str(path),'exec'), namespace)

async def survey():
    class Agent: pass
    class Task: pass
    agent=SimpleNamespace(id='fixture-agent',name='Fixture',description='',skills=[],
                          capabilities={},mode='task',group='',workspace_path='')
    task=SimpleNamespace(id='fixture-task',agent_id=agent.id,context_id='fixture-context',
                         status='completed',updated_at=datetime(2026,11,11,tzinfo=timezone.utc),
                         artifacts=[],message={'kind':'message','role':'user','messageId':'m1','parts':[]},
                         error='',trace_id='fixture-trace')
    class DB:
        async def get(self, model, key):
            if model is Agent:return agent if key==agent.id else None
            if model is Task:return task if key==task.id else None
            raise AssertionError('Unexpected dependency')
    class Request:
        def __init__(self, payload):self.payload=payload
        async def json(self):return self.payload
    ns={'Any':object,'Agent':Agent,'A2ATask':Task}
    load_functions(SERVICE,['build_agent_card','task_to_a2a_dict','cancel_task'],ns)
    captured=[]
    async def fake_send(**kwargs):
        captured.append({k:v for k,v in kwargs.items() if k!='db'})
        return task
    service=SimpleNamespace(send_message=fake_send,cancel_task=ns['cancel_task'],
                            task_to_a2a_dict=ns['task_to_a2a_dict'])
    routes={'Any':object,'Agent':Agent,'A2ATask':Task,'a2a_service':service,
            'logger':logging.getLogger('survey')}
    routes['logger'].disabled=True
    load_functions(ROUTE,['_jsonrpc_error','_jsonrpc_result','agent_jsonrpc'],routes)
    db=DB(); checks=[]
    def record(key,expect,actual,ok):
        checks.append({'id':key,'expected':expect,'actual':actual,'meets_expectation':bool(ok)})
    async def call(method,params,**overrides):
        payload={'jsonrpc':'2.0','id':1,'method':method,'params':params,**overrides}
        return await routes['agent_jsonrpc'](agent.id,Request(payload),db)
    card=ns['build_agent_card'](agent,'http://fixture.invalid')
    record('card-protocol-version','explicit supported protocolVersion',card.get('protocolVersion'),bool(card.get('protocolVersion')))
    record('provider-url','provider.url required when provider is included',card.get('provider',{}).get('url'),bool(card.get('provider',{}).get('url')))
    response=await call('tasks/get',{'id':task.id})
    record('task-query-identity',task.id,response.get('result',{}).get('id'),response.get('result',{}).get('id')==task.id)
    record('task-discriminator','kind=task',response.get('result',{}).get('kind'),response.get('result',{}).get('kind')=='task')
    response=await call('tasks/get',{'id':'absent'})
    record('unknown-task-error',-32001,response.get('error',{}).get('code'),response.get('error',{}).get('code')==-32001)
    response=await call('tasks/get',{'id':task.id},jsonrpc='1.0')
    record('invalid-jsonrpc-version',-32600,response.get('error',{}).get('code'),response.get('error',{}).get('code')==-32600)
    response=await call('tasks/get',['bad-params'])
    record('invalid-method-params',-32602,response.get('error',{}).get('code'),response.get('error',{}).get('code')==-32602)
    response=await call('tasks/cancel',{'id':task.id})
    record('cancel-terminal-task',-32002,response.get('error',{}).get('code'),response.get('error',{}).get('code')==-32002)
    message={'kind':'message','role':'user','messageId':'fixture-m2','contextId':'existing-context',
             'parts':[{'kind':'text','text':'synthetic fixture; no model dispatch'}]}
    await call('message/send',{'message':message})
    record('message-context-mapping','existing-context',captured[-1]['context_id'],captured[-1]['context_id']=='existing-context')
    agent.capabilities={'streaming':True}
    advertised=ns['build_agent_card'](agent,'http://fixture.invalid')['capabilities']['streaming']
    response=await call('message/stream',{'message':message})
    code=response.get('error',{}).get('code')
    record('streaming-advertisement','declared support corresponds to implemented method',
           {'advertised':advertised,'error':code},not advertised or code!=-32601)
    response=await call('fixture/unknown',{})
    record('unknown-method-error',-32601,response.get('error',{}).get('code'),response.get('error',{}).get('code')==-32601)
    return {'date':'2026-11-11','baseline':'A2A 0.3.0 JSON-RPC; bounded legacy compatibility survey',
            'specification':'https://a2a-protocol.org/v0.3.0/specification/',
            'method':'isolated original function bodies with in-memory dependencies; no HTTP/auth/runtime tests',
            'no_database_or_model_calls':True,
            'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [SERVICE,ROUTE]},
            'checks':checks,'met':sum(c['meets_expectation'] for c in checks),
            'gaps':sum(not c['meets_expectation'] for c in checks)}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=asyncio.run(survey())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'checks':len(result['checks']),'met':result['met'],'gaps':result['gaps'],'output':str(args.output)}))
