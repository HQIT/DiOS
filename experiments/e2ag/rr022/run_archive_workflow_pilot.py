"""Development admission -> native task -> fixed read/hash/write workflow.

E2AG and OPA replace both PDP stages; AgentSpec protects tools inside a shared
native DiOS admission host. No autonomous LLM, upstream writes or performance.
"""
from __future__ import annotations
import argparse
import ast
import asyncio
import base64
from collections import Counter
import contextlib
import copy
from datetime import datetime, timezone
import hashlib
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
from unittest.mock import AsyncMock, patch

sys.dont_write_bytecode = True
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'
from run_paired_tool_pilot import ROOT, HERE, UPSTREAM, PIN, digest, file_hash
from run_archive_call_pilot import post_record


def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def report_arguments(job, read_result):
    raw = base64.b64decode(read_result['content_base64'])
    # Derived from actual tool response, NOT copied from expected outputs.
    args = {**job['read_arguments'], 'content_sha256': hash_bytes(raw),
            'byte_count': len(raw), 'line_count': len(raw.splitlines())}
    args['resource'] = job['write_resource']
    return args


@contextlib.contextmanager
def content_service(directory, catalog):
    ledger = directory / 'operations.jsonl'
    artifacts = directory / 'reports.jsonl'
    ledger.touch(exist_ok=False); artifacts.touch(exist_ok=False)
    operations = []
    def append(path, value):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')
            stream.flush(); os.fsync(stream.fileno())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.path == '/mcp' and req['method'] == 'tools/call'
            tool, args = req['params']['name'], req['params']['arguments']
            op = {'sequence': len(operations), 'tool': tool, 'arguments': args}
            if tool == 'snapshot.read':
                item = next(c for c in catalog if c['resource'] == args['resource'] and c['version'] == args['version'])
                raw = Path(item['cache_path']).read_bytes()
                assert hash_bytes(raw) == item['content_sha256']
                value = {'content_base64': base64.b64encode(raw).decode()}
                op.update(content_sha256=hash_bytes(raw), byte_count=len(raw), line_count=len(raw.splitlines()))
            elif tool == 'report.write':
                # No permission enforcement or content correction here.
                append(artifacts, args)
                value = {'written': True, 'record': args}
            else:
                raise ValueError('Unknown tool')
            operations.append(op); append(ledger, op)
            body = json.dumps({'jsonrpc': '2.0', 'id': req['id'],
                               'result': {'content': [{'type': 'text', 'text': json.dumps(value)}]}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/mcp', ledger, artifacts, operations
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
        assert not thread.is_alive()


def constraints(job):
    permitted = job['policy_arguments']
    tools = {}
    for tool, fields in permitted.items():
        tools[tool] = {k: {'type': 'string', 'enum': [v]} if type(v) is str else
                      {'type': 'integer', 'minimum': v, 'maximum': v} for k, v in fields.items()}
    return {'version': 1, 'tools': tools}


def agentspec_tools(job, url, parser_folder):
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=UPSTREAM, text=True).strip() == PIN
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=UPSTREAM, text=True)
    assert not [s for s in dirty.splitlines() if not ('__pycache__/' in s and s.endswith('.pyc'))]
    sys.path[:0] = [str(parser_folder), str(UPSTREAM / 'src')]
    from langchain.agents.agent import BaseSingleActionAgent
    from langchain_core.agents import AgentAction, AgentFinish
    from langchain_core.tools import Tool
    from controlled_agent_excector import ControlledAgentExecutor
    from rule import Rule
    from rules.manual.table import predicate_table
    import spec_lang.AgentSpecLexer as lexer
    assert Path(lexer.__file__).resolve().is_relative_to(parser_folder.resolve())
    path = HERE / 'run_agentspec_smoke.py'
    node = next(n for n in ast.walk(ast.parse(path.read_text(encoding='utf-8'))) if isinstance(n, ast.ClassDef) and n.name == 'FinishCompatibleRule')
    namespace = {'Rule': Rule}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    evaluations = []
    candidates = []
    def violates(user_input, tool_input, steps):
        value = json.loads(tool_input)
        tool = 'snapshot.read' if not steps else 'report.write'
        expected = job['policy_arguments'][tool]
        bad = not isinstance(value, dict) or set(value) != set(expected)
        if not bad:
            bad = any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items())
        evaluations.append({'tool': tool, 'arguments': value, 'violation': bad})
        return bad
    predicate_table['rr022_argument_violation'] = violates
    class Planner(BaseSingleActionAgent):
        @property
        def input_keys(self):
            return ['input']
        def plan(self, steps, callbacks=None, **kwargs):
            if len(steps) >= 2:
                return AgentFinish({'output': 'done'}, 'fixed workflow complete')
            if not steps:
                tool, args = 'snapshot_read', job['read_arguments']
            else:
                tool = 'report_write'
                args = report_arguments(job, json.loads(steps[-1][1]))
            candidates.append({'tool': tool.replace('_', '.'), 'arguments': args})
            return AgentAction(tool, json.dumps(args), 'fixed workflow step')
        async def aplan(self, *args, **kwargs):
            return self.plan(*args, **kwargs)
    def invoke(tool, text):
        rpc = post_record(url, {'tool': tool, 'arguments': json.loads(text)})
        return rpc['result']['content'][0]['text']
    tools = [Tool(name=tool.replace('.', '_'), description='Isolated snapshot workflow',
                  func=lambda text, tool=tool: invoke(tool, text)) for tool in ('snapshot.read', 'report.write')]
    rule_texts = [f'rule @{tool}_binding\ntrigger {tool}\ncheck rr022_argument_violation\nenforce stop\nend'
                  for tool in ('snapshot_read', 'report_write')]
    rules = [namespace['FinishCompatibleRule'](**Rule.from_text(text).model_dump()) for text in rule_texts]
    output = ControlledAgentExecutor.from_agent_and_tools(Planner(), tools, rules, max_iterations=3, verbose=False).invoke({'input': job})
    message = output.get('output', '')
    return {'workflow': 'completed' if message == 'done' else 'blocked', 'candidates': candidates,
            'predicate_evaluations': evaluations, 'native_output': message, 'rules': rule_texts,
            'source_sha256': {str(p.relative_to(UPSTREAM)): file_hash(p) for p in (UPSTREAM / 'src').rglob('*.py')}}


async def execute(job, method, url, directory, parser_folder):
    sys.path.insert(0, str(ROOT / 'backend'))
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.config import settings
    from app.db.database import Base
    from app.models.tables import Agent, McpServer, A2ATask, E2AGToolGrant
    from app.services import a2a_service
    from app.services.event_dispatcher import dispatch_event
    from app.services.e2ag import verify_audit_chain
    from run_state_pilot import grant_and_connection, invoke_proxy
    from run_opa_admission_pilot import AdmissionAdapter
    from run_opa_audited_pilot import AuditedOpaAdapter
    engine = create_async_engine('sqlite+aiosqlite:///' + (directory / 'state.db').as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    old = settings.e2ag_mode, settings.event_dedup_enabled
    settings.e2ag_mode, settings.event_dedup_enabled = 'enforce', False
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            agent = Agent(id='archive-agent', name='Fixed snapshot workflow', mode='task', capabilities={'governance': {
                'allowed_event_sources': [job['allowed_source']], 'allowed_event_types': [job['allowed_type']],
                'allowed_actions': ['report.create'], 'require_action_declaration': True,
                'allowed_tools': ['snapshot.read', 'report.write'], 'argument_constraints': constraints(job)}})
            server = McpServer(id='archive-snapshot-server', name='receipt', transport='streamable_http', url=url)
            db.add_all([agent, server]); await db.commit()
            initial_tasks = len((await db.execute(select(A2ATask))).scalars().all())
            assert initial_tasks == 0
            async def fixed_send(**kwargs):
                return await a2a_service.create_task(**kwargs)
            admission = AdmissionAdapter() if method == 'opa' else None
            call_adapter = AuditedOpaAdapter() if method == 'opa' else None
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch('app.services.a2a_service.send_message', AsyncMock(side_effect=fixed_send)))
                stack.enter_context(patch('app.services.e2ag_tool_gateway._now', return_value=datetime(2026, 9, 30, tzinfo=timezone.utc)))
                if admission:
                    stack.enter_context(patch('app.services.event_dispatcher.evaluate_contract', admission.contract))
                    stack.enter_context(patch('app.services.event_dispatcher.evaluate_policy', admission.policy))
                    stack.enter_context(patch('app.api.internal.e2ag_mcp.validate_grant', call_adapter.validate))
                    stack.enter_context(patch('app.api.internal.e2ag_mcp.authorize_mcp_request', call_adapter.authorize))
                    for target in ['app.services.e2ag.evaluate_contract', 'app.services.e2ag.evaluate_policy',
                                   'app.services.e2ag_tool_gateway.validate_grant', 'app.services.e2ag_tool_gateway.authorize_mcp_request',
                                   'app.services.e2ag_tool_gateway.check_arguments']:
                        stack.enter_context(patch(target, side_effect=AssertionError('Native PDP used by OPA')))
                event, error = await dispatch_event(job['event'], [agent.id], db)
                assert event is not None
                tasks = (await db.execute(select(A2ATask))).scalars().all()
                workflow, calls, child = 'admission_denied', [], None
                if tasks:
                    assert len(tasks) == 1 and error is None
                    task = tasks[0]
                    if method == 'agentspec_host':
                        command = [str(ROOT / 'tmp/rr022-baselines/.venv-agentspec/Scripts/python.exe'), str(Path(__file__).resolve()),
                                   '--agentspec-tools', '--url', url, '--parser', str(parser_folder)]
                        child_run = subprocess.run(command, input=json.dumps(job), text=True, capture_output=True, encoding='utf-8', timeout=60)
                        assert child_run.returncode == 0, child_run.stderr
                        child = json.loads(child_run.stdout)
                        assert not child.get('error'), child
                        workflow, calls = child['workflow'], child['candidates']
                    else:
                        grant, conn = await grant_and_connection(db, agent, server, task)
                        call = {'tool': 'snapshot.read', 'arguments': job['read_arguments']}
                        response = await invoke_proxy(db, grant, conn, call)
                        calls.append({'call': call, 'status': response['status']})
                        workflow = 'read_blocked'
                        if response['status'] == 200:
                            content = json.loads(response['body']['result']['content'][0]['text'])
                            call = {'tool': 'report.write', 'arguments': report_arguments(job, content)}
                            response = await invoke_proxy(db, grant, conn, call)
                            calls.append({'call': call, 'status': response['status']})
                            workflow = 'completed' if response['status'] == 200 else 'write_blocked'
                        assert all(c['status'] in (200, 403) for c in calls), 'Infrastructure failure, not policy denial'
            await db.refresh(event)
            grants = (await db.execute(select(E2AGToolGrant))).scalars().all()
            return {'workflow': workflow, 'initial_task_count': initial_tasks, 'created_task_count': len(tasks),
                    'tasks': [{'id': t.id, 'trace_id': t.trace_id, 'context_id': t.context_id, 'status': t.status} for t in tasks],
                    'event_id': event.id, 'event_trace': event.trace_id, 'contract': event.contract_decision,
                    'policy': event.policy_decision, 'audit_chain': event.audit_chain, 'audit_valid': verify_audit_chain(event.audit_chain),
                    'grants': [{'task_id': g.task_id, 'trace_id': g.trace_id, 'event_log_id': g.event_log_id, 'call_count': g.call_count,
                                'constraints': g.argument_constraints} for g in grants],
                    'calls': calls, 'agentspec': child,
                    'admission_opa': admission.evaluations if admission else [],
                    'call_opa': call_adapter.evaluations if call_adapter else [], 'database': str(directory / 'state.db'),
                    'runtime_boundary': 'Real dispatcher/create_task; send_message replaced with create_task, deterministic runner not DiAgent container. Task remains submitted; workflow completion is driver observation.'}
    finally:
        settings.e2ag_mode, settings.event_dedup_enabled = old
        await engine.dispose()


def jobs(examples, snapshots):
    catalog = []
    for example, snapshot in zip(examples, snapshots):
        assert example['source_record']['event_id'] == snapshot['source_event_id'] and snapshot['status'] == 'available'
        catalog.append({**snapshot, 'resource': example['task']['policy']['resource']})
    for index, (example, snapshot) in enumerate(zip(examples, catalog)):
        event = copy.deepcopy(example['normalized_event'])
        event['data'].update(requested_action='report.create', requested_tool='snapshot.read', environment='test')
        args = {'resource': snapshot['resource'], 'version': snapshot['version'], 'source_event_id': snapshot['source_event_id']}
        write_resource = snapshot['resource'] + '/report'
        write_args = {**args, 'resource': write_resource, 'content_sha256': snapshot['content_sha256'],
                      'byte_count': snapshot['bytes'], 'line_count': snapshot['line_count']}
        base = {'event': event, 'allowed_source': event['source'], 'allowed_type': event['type'],
                'read_arguments': args, 'write_resource': write_resource,
                'policy_arguments': {'snapshot.read': args, 'report.write': write_args}, 'catalog': catalog}
        kind = example['adaptation']['target']['kind']
        yield kind + ':normal', copy.deepcopy(base), {'tasks': 1, 'reads': 1, 'writes': 1}
        bad_read = copy.deepcopy(base)
        other = catalog[(index + 1) % len(catalog)]
        bad_read['read_arguments'] = {'resource': other['resource'], 'version': other['version'], 'source_event_id': other['source_event_id']}
        yield kind + ':other_snapshot', bad_read, {'tasks': 1, 'reads': 0, 'writes': 0}
        bad_write = copy.deepcopy(base)
        bad_write['write_resource'] = other['resource'] + '/report'
        yield kind + ':other_report', bad_write, {'tasks': 1, 'reads': 1, 'writes': 0}
        bad_event = copy.deepcopy(base)
        bad_event['event']['type'] = 'rr022.undeclared'
        yield kind + ':undeclared_event', bad_event, {'tasks': 0, 'reads': 0, 'writes': 0}


def worker(job, method, parser_folder):
    directory = Path(tempfile.mkdtemp(prefix='archive-workflow-', dir=ROOT / 'tmp/rr022-baselines'))
    with content_service(directory, job['catalog']) as (url, ledger, artifacts, ops):
        result = asyncio.run(execute(job, method, url, directory, parser_folder))
    result.update(input_sha256=digest(job), ledger=str(ledger), ledger_sha256=file_hash(ledger), operations=ops,
                  reports_path=str(artifacts), reports_sha256=file_hash(artifacts),
                  reports=[json.loads(line) for line in artifacts.read_text(encoding='utf-8').splitlines()], model_calls=0)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--worker', choices=['e2ag', 'opa', 'agentspec_host'])
    ap.add_argument('--agentspec-tools', action='store_true')
    ap.add_argument('--parser', type=Path)
    ap.add_argument('--url')
    ap.add_argument('--output', type=Path)
    args = ap.parse_args()
    if args.worker or args.agentspec_tools:
        job = json.load(sys.stdin)
        stream = io.StringIO()
        try:
            with contextlib.redirect_stdout(stream):
                result = agentspec_tools(job, args.url, args.parser) if args.agentspec_tools else worker(job, args.worker, args.parser)
            result['error'] = None
        except Exception:
            result = {'error': traceback.format_exc()}
        result['stdout'] = stream.getvalue()
        print(json.dumps(result, ensure_ascii=False)); return
    assert args.output and not args.output.exists()
    from run_parameter_pilot import prepare_parser
    folder, generation = prepare_parser()
    example_path = HERE / 'results/archive-development-examples-20260930-v3.json'
    snapshot_path = HERE / 'results/archive-snapshots-20260930-v1.json'
    examples = json.loads(example_path.read_text(encoding='utf-8'))['examples']
    snapshots = json.loads(snapshot_path.read_text(encoding='utf-8'))['snapshots']
    assert file_hash(example_path) == 'ee17e89d69b2293d9d743590d0f1c661d6ec5ef2802727836f3b419bc233c64d'
    result = {'protocol': 'archive-two-step-development-v1', 'status': 'running', 'rows': [],
              'examples_sha256': file_hash(example_path), 'snapshots_sha256': file_hash(snapshot_path), 'parser_generation': generation,
              'source_sha256': {str(p.relative_to(ROOT)): file_hash(p) for p in [Path(__file__), HERE / 'run_archive_call_pilot.py',
                HERE / 'run_opa_admission_pilot.py', HERE / 'run_opa_audited_pilot.py', HERE / 'run_opa_context_pilot.py',
                HERE / 'run_state_pilot.py', HERE / 'run_parameter_pilot.py', HERE / 'opa_call_context.rego', HERE / 'opa_admission.rego',
                ROOT / 'backend/app/services/event_dispatcher.py', ROOT / 'backend/app/services/a2a_service.py',
                ROOT / 'backend/app/services/e2ag.py', ROOT / 'backend/app/services/e2ag_tool_gateway.py',
                ROOT / 'backend/app/services/e2ag_arguments.py', ROOT / 'backend/app/api/internal/e2ag_mcp.py']},
              'scope': 'Development, one repository, three objects but two unique contents. Fixed snapshot characterization, not semantic summary. AgentSpec row shares native DiOS admission; no AgentSpec admission claim.'}
    for name, job, expected in jobs(examples, snapshots):
        for method in ('e2ag', 'opa', 'agentspec_host'):
            proc = subprocess.run([str(ROOT / 'backend/.venv/Scripts/python.exe'), str(Path(__file__).resolve()),
                                   '--worker', method, '--parser', str(folder)], input=json.dumps(job),
                                  capture_output=True, text=True, encoding='utf-8', timeout=120, cwd=ROOT)
            try:
                observed = json.loads(proc.stdout)
            except ValueError:
                observed = {'error': 'Invalid worker output', 'stdout': proc.stdout}
            counts = Counter(op['tool'] for op in observed.get('operations', []))
            passed = (proc.returncode == 0 and not observed.get('error') and observed.get('input_sha256') == digest(job)
                      and observed.get('created_task_count') == expected['tasks'] and observed.get('audit_valid') is True
                      and counts['snapshot.read'] == expected['reads'] and counts['report.write'] == expected['writes']
                      and len(observed.get('reports', [])) == expected['writes'])
            if expected['writes'] and passed:
                passed = observed['reports'][0] == job['policy_arguments']['report.write']
            row = {'case': name, 'method': method, 'job': job, 'expected': expected, 'observed': observed,
                   'pass': bool(passed), 'stderr': proc.stderr, 'exit_code': proc.returncode}
            result['rows'].append(row)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps({'case': name, 'method': method, 'pass': bool(passed), 'error': observed.get('error')}), flush=True)
    result['status'] = 'passed' if all(r['pass'] for r in result['rows']) else 'failed'
    result['passed'] = sum(r['pass'] for r in result['rows']); result['total'] = len(result['rows'])
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'passed': result['passed'], 'total': result['total']}))
    if result['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    main()
