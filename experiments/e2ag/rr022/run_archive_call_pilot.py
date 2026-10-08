"""Three-method call-boundary pilot with real loopback HTTP and disk receipts.

Uses frozen development examples. Preloaded task/event, NOT event admission,
real planner or full MCP session protocol. No external writes or model calls.
"""
from __future__ import annotations
import argparse
import ast
import asyncio
import contextlib
import copy
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.request
from unittest.mock import patch

sys.dont_write_bytecode = True
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'
from run_paired_tool_pilot import ROOT, HERE, UPSTREAM, PIN, digest, file_hash


@contextlib.contextmanager
def record_service(directory, source_event):
    """Accept every syntactically valid tools/call; no permission predicate."""
    ledger = directory / 'tool-receipts.jsonl'
    ledger.touch(exist_ok=False)
    arrivals = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            arrivals.append(copy.deepcopy(request))
            assert self.path == '/mcp' and request['method'] == 'tools/call'
            record = {'sequence': len(arrivals), 'tool': request['params']['name'],
                      'arguments': request['params'].get('arguments'),
                      'source_snapshot': source_event}
            with ledger.open('a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
                f.flush()
                os.fsync(f.fileno())
            body = json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                               'result': {'content': [{'type': 'text', 'text': json.dumps(record)}]}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/mcp', ledger, arrivals
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def post_record(url, call):
    assert url.startswith('http://127.0.0.1:') and url.endswith('/mcp')
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': call['tool'], 'arguments': call['arguments']}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as response:
        return json.load(response)


def native_constraints(policy):
    return {'version': 1, 'tools': {policy['tool']: {
        **{k: {'type': 'string', 'enum': [policy[k]]} for k in ('resource', 'version', 'source_event_id')},
        'record_count': {'type': 'integer', 'minimum': policy['record_count'], 'maximum': policy['record_count']}}}}


async def run_e2ag(job, method, url, directory):
    sys.path.insert(0, str(ROOT / 'backend'))
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.config import settings
    from app.db.database import Base
    from app.models.tables import Agent, McpServer, A2ATask, EventLog
    from app.services.e2ag import verify_audit_chain
    from run_state_pilot import grant_and_connection, invoke_proxy
    from run_opa_audited_pilot import AuditedOpaAdapter
    now = datetime.fromisoformat(job['host']['now'])
    engine = create_async_engine('sqlite+aiosqlite:///' + (directory / 'state.db').as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    old_mode = settings.e2ag_mode
    settings.e2ag_mode = 'enforce'
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            e = job['event']
            agent = Agent(id=job['task']['agent_id'], name='Archive metadata pilot', mode='task',
                          capabilities={'governance': {'allowed_tools': [job['policy']['tool']],
                                                       'argument_constraints': native_constraints(job['policy'])}})
            server = McpServer(id='archive-sink', name='receipt', transport='streamable_http', url=url)
            event = EventLog(id=e['id'], source=e['source'], event_type=e['type'], cloud_event=e,
                             trace_id=job['task']['trace_id'], status='dispatched')
            task = A2ATask(**job['task'], context_id=event.id, message={'content': 'fixed event registration'})
            db.add_all([agent, server, event, task])
            await db.commit()
            with patch('app.services.e2ag_tool_gateway._now', return_value=now):
                grant, conn = await grant_and_connection(db, agent, server, task)
                grant.expires_at = now + timedelta(seconds=300)
                await db.commit()
                await db.refresh(event)
                before = len(event.audit_chain)
                adapter = AuditedOpaAdapter() if method == 'opa' else None
                with contextlib.ExitStack() as stack:
                    if adapter:
                        stack.enter_context(patch('app.api.internal.e2ag_mcp.validate_grant', adapter.validate))
                        stack.enter_context(patch('app.api.internal.e2ag_mcp.authorize_mcp_request', adapter.authorize))
                        for name in ('validate_grant', 'authorize_mcp_request', 'check_arguments'):
                            stack.enter_context(patch('app.services.e2ag_tool_gateway.' + name,
                                                      side_effect=AssertionError('E2AG predicate used by OPA')))
                    response = await invoke_proxy(db, grant, conn, job['call'])
            await db.refresh(event)
            await db.refresh(grant)
            return {'decision': {200: 'allow', 403: 'deny'}.get(response['status'], 'error'),
                    'response': response, 'audit_chain': event.audit_chain,
                    'call_audit': event.audit_chain[before:], 'audit_valid': verify_audit_chain(event.audit_chain),
                    'grant_call_count': grant.call_count, 'grant_status': grant.status,
                    'constraints': grant.argument_constraints,
                    'opa_evaluations': adapter.evaluations if adapter else [],
                    'database': str(directory / 'state.db'),
                    'boundary': 'Preloaded event/task; real issuance, POST handler, loopback HTTP, persistent DB. No event admission.'}
    finally:
        settings.e2ag_mode = old_mode
        await engine.dispose()


def run_agentspec(job, parser_folder, url):
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=UPSTREAM, text=True).strip() == PIN
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=UPSTREAM, text=True)
    assert not [line for line in dirty.splitlines() if not ('__pycache__/' in line and line.endswith('.pyc'))]
    sys.path[:0] = [str(parser_folder), str(UPSTREAM / 'src')]
    from langchain.agents.agent import BaseSingleActionAgent
    from langchain_core.agents import AgentAction, AgentFinish
    from langchain_core.tools import Tool
    from controlled_agent_excector import ControlledAgentExecutor
    from rule import Rule
    from rules.manual.table import predicate_table
    import spec_lang.AgentSpecLexer as lexer_module
    assert Path(lexer_module.__file__).resolve().is_relative_to(parser_folder.resolve())
    compat = HERE / 'run_agentspec_smoke.py'
    node = next(n for n in ast.walk(ast.parse(compat.read_text(encoding='utf-8')))
                if isinstance(n, ast.ClassDef) and n.name == 'FinishCompatibleRule')
    scope = {'Rule': Rule}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(compat), 'exec'), scope)
    policy = copy.deepcopy(job['policy'])
    evaluations = []
    def violates(user_input, tool_input, intermediate_steps):
        # Independent implementation under native interpreter; no imported oracle.
        try:
            value = json.loads(tool_input)
        except (ValueError, TypeError):
            value = None
        invalid = not isinstance(value, dict) or set(value) != {'resource', 'version', 'source_event_id', 'record_count'}
        if not invalid:
            invalid = any(type(value[k]) is not str or value[k] != policy[k]
                          for k in ('resource', 'version', 'source_event_id'))
            invalid = invalid or type(value['record_count']) is not int or value['record_count'] != policy['record_count']
        evaluations.append({'arguments': value, 'violation': invalid})
        return invalid
    predicate_table['rr022_argument_violation'] = violates
    class Planner(BaseSingleActionAgent):
        @property
        def input_keys(self):
            return ['input']

        def plan(self, intermediate_steps, callbacks=None, **kwargs):
            if intermediate_steps:
                return AgentFinish({'output': 'done'}, 'fixed finish')
            return AgentAction('event_record', json.dumps(kwargs['input']['call']['arguments']), 'fixed archived call')

        async def aplan(self, *args, **kwargs):
            return self.plan(*args, **kwargs)
    assert job['call']['tool'] == policy['tool'] == 'event.record'
    tools = [Tool(name='event_record', description='Local metadata registration',
                  func=lambda text: json.dumps(post_record(url, {'tool': 'event.record', 'arguments': json.loads(text)})))]
    rule_text = 'rule @archive_binding\ntrigger event_record\ncheck rr022_argument_violation\nenforce stop\nend'
    rule = scope['FinishCompatibleRule'](**Rule.from_text(rule_text).model_dump())
    output = ControlledAgentExecutor.from_agent_and_tools(Planner(), tools, [rule], max_iterations=2, verbose=False).invoke({'input': job})
    message = output.get('output', '')
    return {'decision': 'allow' if message == 'done' else 'deny' if message.startswith('action stopped by rule ') else 'error',
            'native_output': message, 'native_rule': rule_text, 'predicate_evaluations': evaluations,
            'upstream_commit': PIN, 'compat_sha256': file_hash(compat),
            'source_sha256': {str(p.relative_to(UPSTREAM)): file_hash(p) for p in (UPSTREAM / 'src').rglob('*.py')},
            'audit_chain': None, 'boundary': 'Native DSL/interpreter/executor plus independent custom predicate and finish compatibility. Driver log is not native durable audit.'}


def jobs(examples):
    for example in examples:
        task = example['task']
        for case in example['expected_cases']:
            job = {'policy': task['policy'], 'event': example['normalized_event'], 'call': case['call'],
                   'task': {'id': 'task-archive', 'agent_id': 'agent-archive', 'trace_id': 'trace-archive', 'status': 'working'},
                   'host': {'now': '2026-09-30T00:00:00+00:00', 'grant_status': 'active', 'ttl_seconds': 300}}
            yield example['adaptation']['target']['kind'] + ':' + case['name'], False, copy.deepcopy(job), case['expected_receipts']
    example = next(e for e in examples if e['adaptation']['target']['kind'] == 'pr')
    base = {'policy': example['task']['policy'], 'event': example['normalized_event'], 'call': example['task']['call'],
            'task': {'id': 'task-archive', 'agent_id': 'agent-archive', 'trace_id': 'trace-archive', 'status': 'working'},
            'host': {'now': '2026-09-30T00:00:00+00:00', 'grant_status': 'active', 'ttl_seconds': 300}}
    for key, variant in [('resource', example['expected_cases'][1]['call']['arguments']['resource']),
                         ('version', example['expected_cases'][2]['call']['arguments']['version']),
                         ('source_event_id', examples[0]['source_record']['event_id'])]:
        changed = copy.deepcopy(base)
        changed['call']['arguments'][key] = variant
        if key == 'source_event_id':
            yield 'control:source_id_deny', True, copy.deepcopy(changed), 0
        changed['policy'][key] = variant
        yield 'control:reverse_' + key + '_allow', True, copy.deepcopy(changed), 1
        changed['call'] = copy.deepcopy(base['call'])
        yield 'control:reverse_' + key + '_deny_original', True, changed, 0


def worker(job, method, parser_folder):
    directory = Path(tempfile.mkdtemp(prefix='archive-call-', dir=ROOT / 'tmp/rr022-baselines'))
    with record_service(directory, job['event']) as (url, ledger, arrivals):
        if method == 'agentspec':
            observed = run_agentspec(job, parser_folder, url)
        elif method == 'unguarded':
            observed = {'decision': 'allow', 'response': post_record(url, job['call'])}
        else:
            observed = asyncio.run(run_e2ag(job, method, url, directory))
    records = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
    observed.update(input_sha256=digest(job), ledger=str(ledger), ledger_sha256=file_hash(ledger),
                    records=records, arrivals=arrivals, loopback_url=url, model_calls=0)
    return observed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', choices=['e2ag', 'opa', 'agentspec', 'unguarded'])
    parser.add_argument('--parser', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker:
        job = json.load(sys.stdin)
        assert set(job) == {'policy', 'event', 'call', 'task', 'host'}
        stream = io.StringIO()
        try:
            with contextlib.redirect_stdout(stream):
                result = worker(job, args.worker, args.parser)
            result['error'] = None
        except Exception:
            result = {'error': traceback.format_exc()}
        result['stdout'] = stream.getvalue()
        print(json.dumps(result, ensure_ascii=False))
        return
    assert args.output and not args.output.exists()
    from run_parameter_pilot import prepare_parser
    from run_opa_context_pilot import OPA, OPA_SHA, POLICY
    examples_path = HERE / 'results/archive-development-examples-20260930-v3.json'
    assert file_hash(examples_path) == 'ee17e89d69b2293d9d743590d0f1c661d6ec5ef2802727836f3b419bc233c64d'
    assert file_hash(OPA) == OPA_SHA
    examples = json.loads(examples_path.read_text(encoding='utf-8'))['examples']
    parser_folder, parser_info = prepare_parser()
    interpreters = {'e2ag': ROOT / 'backend/.venv/Scripts/python.exe', 'opa': ROOT / 'backend/.venv/Scripts/python.exe',
                    'agentspec': ROOT / 'tmp/rr022-baselines/.venv-agentspec/Scripts/python.exe',
                    'unguarded': ROOT / 'backend/.venv/Scripts/python.exe'}
    result = {'protocol': 'archive-call-development-v1', 'status': 'running', 'rows': [], 'sink_controls': [],
              'examples_sha256': file_hash(examples_path), 'parser_generation': parser_info,
              'source_sha256': {str(p.relative_to(ROOT)): file_hash(p) for p in [Path(__file__), POLICY,
                   HERE / 'run_parameter_pilot.py', HERE / 'run_opa_context_pilot.py', HERE / 'run_opa_audited_pilot.py',
                   HERE / 'run_state_pilot.py', HERE / 'run_paired_tool_pilot.py', HERE / 'run_agentspec_smoke.py',
                   ROOT / 'backend/app/api/internal/e2ag_mcp.py', ROOT / 'backend/app/services/e2ag_tool_gateway.py',
                   ROOT / 'backend/app/services/e2ag_arguments.py', ROOT / 'backend/app/services/e2ag.py']},
              'opa_binary_sha256': file_hash(OPA), 'agentspec_commit': PIN,
              'scope': 'Three exploratory metadata-registration objects; preloaded tasks, fixed planner/calls, real loopback HTTP and fsynced ledger. No admission, approval, real LLM or full MCP session/performance comparison.'}
    def save():
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    def run(name, job, method, count, control):
        command = [str(interpreters[method]), str(Path(__file__).resolve()), '--worker', method, '--parser', str(parser_folder)]
        proc = subprocess.run(command, input=json.dumps(job), capture_output=True, text=True, encoding='utf-8', timeout=90, cwd=ROOT)
        try:
            observed = json.loads(proc.stdout)
        except ValueError:
            observed = {'error': 'invalid worker JSON', 'stdout': proc.stdout}
        passed = (proc.returncode == 0 and not observed.get('error') and observed.get('input_sha256') == digest(job)
                  and observed.get('decision') == ('allow' if count else 'deny')
                  and len(observed.get('records', [])) == count and len(observed.get('arrivals', [])) == count)
        for rec in observed.get('records', []):
            passed = passed and rec['tool'] == job['call']['tool'] and rec['arguments'] == job['call']['arguments'] and rec['source_snapshot'] == job['event']
        if method in ('e2ag', 'opa'):
            passed = passed and observed.get('audit_valid') is True and len(observed.get('call_audit', [])) == 1 and observed.get('grant_call_count') == 1
        if method == 'opa':
            passed = passed and len(observed.get('opa_evaluations', [])) == 2 and all(not e['error'] for e in observed.get('opa_evaluations', []))
        if method == 'agentspec':
            passed = passed and len(observed.get('predicate_evaluations', [])) == 1
        row = {'case': name, 'control': control, 'method': method, 'job': job, 'expected_records': count,
               'observed': observed, 'pass': bool(passed), 'exit_code': proc.returncode, 'stderr': proc.stderr}
        print(json.dumps({'case': name, 'method': method, 'pass': bool(passed), 'error': observed.get('error')}), flush=True)
        return row
    for name, control, job, count in jobs(examples):
        if not control and name.endswith('different_object'):
            # This intentionally unauthorized call MUST succeed without a gate.
            result['sink_controls'].append(run(name, job, 'unguarded', 1, True))
        for method in ('e2ag', 'agentspec', 'opa'):
            result['rows'].append(run(name, job, method, count, control))
            save()
    result['passed'] = sum(r['pass'] for r in result['rows'])
    result['total'] = len(result['rows'])
    result['status'] = 'passed' if all(r['pass'] for r in result['rows'] + result['sink_controls']) else 'failed'
    save()
    print(json.dumps({'status': result['status'], 'passed': result['passed'], 'total': result['total']}))
    if result['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    main()
