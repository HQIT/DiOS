"""Socket-path qualification only; never summarize these runs as performance."""
import argparse
import asyncio
from collections import Counter
import contextlib
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from run_paired_tool_pilot import ROOT, HERE, file_hash


def serve(config):
    sys.path.insert(0, str(ROOT / 'backend'))
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import select
    import uvicorn
    from app.config import settings
    from app.db.database import Base, get_db
    from app.models.tables import Agent, McpServer, Subscription, A2ATask, E2AGToolGrant, EventLog
    from app.services import a2a_service
    from app.services.e2ag_tool_gateway import issue_task_mcp_config
    from app.api.os.events import router as events_router
    from app.api.internal.e2ag_mcp import router as proxy_router
    settings.e2ag_mode = 'off' if config['mode'] == 'G0' else 'enforce'
    settings.event_dedup_enabled = False
    settings.webhook_secrets = {'github': config['test_secret']}
    settings.e2ag_internal_base_url = config['app_url']
    engine = create_async_engine('sqlite+aiosqlite:///' + config['database'].replace('\\', '/'))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ready, spans = {}, []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            job = config['job']
            db.add(Agent(id='perf-agent', name='Deterministic socket qualification', mode='task', capabilities={'governance': {
                'allowed_event_sources': [job['allowed_source']], 'allowed_event_types': [job['allowed_type']],
                'allowed_actions': ['report.create'], 'require_action_declaration': True,
                'allowed_tools': ['snapshot.read', 'report.write'], 'argument_constraints': job['constraints']}}))
            db.add(McpServer(id='perf-tool', name='receipt', transport='streamable_http', url=config['upstream_url']))
            db.add(Subscription(agent_id='perf-agent', source_pattern=job['allowed_source'], event_types=[job['allowed_type']], enabled=True))
            await db.commit()
        async def fixed_send(**kwargs):
            started = time.perf_counter_ns()
            task = await a2a_service.create_task(**kwargs)
            created = time.perf_counter_ns()
            db = kwargs['db']
            if config['mode'] == 'G2':
                agent, server = await db.get(Agent, task.agent_id), await db.get(McpServer, 'perf-tool')
                connections, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
                assert not omissions
                connection = connections['receipt']
            else:
                # G1 uses full native admission, but explicitly omits call PEP.
                connection = {'url': config['upstream_url'], 'headers': {}}
            ended = time.perf_counter_ns()
            ready[task.context_id] = dict(task_id=task.id, connection=connection)
            spans.append(dict(stage='task_and_optional_grant', event_id=task.context_id,
                              start_ns=started, task_committed_ns=created, end_ns=ended))
            return task
        with patch.object(a2a_service, 'send_message', fixed_send):
            yield
        await engine.dispose()
    app = FastAPI(lifespan=lifespan)
    async def dependency():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = dependency
    app.include_router(events_router, prefix='/api/os')
    app.include_router(proxy_router, prefix='/api')
    @app.middleware('http')
    async def timing(request, call_next):
        started = time.perf_counter_ns()
        response = await call_next(request)
        spans.append(dict(stage='http_route', path=request.url.path, status=response.status_code,
                          start_ns=started, end_ns=time.perf_counter_ns()))
        return response
    @app.get('/probe/ready')
    async def health(): return {'ready': True}
    @app.get('/probe/event/{event_id}')
    async def event_result(event_id: str):
        async with sessions() as db:
            event = await db.get(EventLog, event_id)
            tasks = list((await db.execute(select(A2ATask))).scalars())
            grants = list((await db.execute(select(E2AGToolGrant))).scalars())
            return dict(ready=ready.get(event_id), chain=event.audit_chain,
                        tasks=[dict(id=t.id, event_id=t.context_id, trace_id=t.trace_id, status=t.status) for t in tasks],
                        grants=[dict(id=g.id, task_id=g.task_id, call_count=g.call_count) for g in grants], spans=spans)
    uvicorn.run(app, host='127.0.0.1', port=config['port'], log_level='error', access_log=False)


def probe(mode, case, job, example):
    import httpx
    from run_archive_workflow_pilot import content_service
    from run_issue_intake_pilot import report_arguments
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.services.e2ag import verify_audit_chain
    directory = Path(tempfile.mkdtemp(prefix='perf-socket-probe-', dir=ROOT / 'tmp'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    config_path = directory / 'config.json'
    with content_service(directory, job['catalog']) as (url, ledger, reports, operations):
        config = dict(mode=mode, job=job, port=port, app_url=base, upstream_url=url,
                      test_secret='isolated-socket-qualification-only', database=str(directory / 'state.db'))
        config_path.write_text(json.dumps(config), encoding='utf-8')
        proc = subprocess.Popen([str(ROOT / 'backend/.venv/Scripts/python.exe'), str(Path(__file__).resolve()),
                                 '--serve', str(config_path)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            with httpx.Client(trust_env=False, timeout=30) as client:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        if client.get(base + '/probe/ready').status_code == 200: break
                    except httpx.TransportError: pass
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Local qualification server not ready')
                    time.sleep(0.1)
                payload = copy.deepcopy(example['adaptation']['projected_payload'])
                payload.update(requested_action='outside-policy' if case == 'admission_denied' else 'report.create',
                               requested_tool='snapshot.read', environment='test')
                raw = json.dumps(payload).encode()
                signature = hmac.new(config['test_secret'].encode(), raw, hashlib.sha256).hexdigest()
                started = time.perf_counter_ns()
                response = client.post(base + '/api/os/events/webhook/github', content=raw,
                                       headers={'content-type': 'application/json', 'x-github-event': 'issues', 'x-hub-signature-256': 'sha256=' + signature})
                admitted = time.perf_counter_ns()
                response.raise_for_status()
                event = response.json(); event_id = event['event_id']
                observed = client.get(base + '/probe/event/' + event_id).json()
                calls = []
                if observed['ready']:
                    conn = observed['ready']['connection']
                    candidate = copy.deepcopy(job['read_arguments'])
                    if case == 'other_read_object': candidate['resource'] += '-test-alias'
                    call_started = time.perf_counter_ns()
                    read = client.post(conn['url'], headers=conn['headers'], json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'snapshot.read', 'arguments': candidate}})
                    calls.append(dict(tool='snapshot.read', status=read.status_code, start_ns=call_started, end_ns=time.perf_counter_ns()))
                    if read.status_code == 200:
                        content = json.loads(read.json()['result']['content'][0]['text'])
                        args = report_arguments(job, content)
                        call_started = time.perf_counter_ns()
                        write = client.post(conn['url'], headers=conn['headers'], json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'report.write', 'arguments': args}})
                        calls.append(dict(tool='report.write', status=write.status_code, start_ns=call_started, end_ns=time.perf_counter_ns()))
                done = time.perf_counter_ns()
                observed = client.get(base + '/probe/event/' + event_id).json()
                observed.pop('ready')  # Never persist the ephemeral grant credential.
            expected_tasks = int(case != 'admission_denied' or mode == 'G0')
            expected_tools = int(expected_tasks and not (case == 'other_read_object' and mode == 'G2'))
            assert len(observed['tasks']) == expected_tasks
            assert len(observed['grants']) == (expected_tasks if mode == 'G2' else 0)
            assert Counter(op['tool'] for op in operations) == Counter({'snapshot.read': expected_tools, 'report.write': expected_tools})
            assert verify_audit_chain(observed['chain'])
            assert sum(e['stage'] == 'tool_call' for e in observed['chain']) == (len(calls) if mode == 'G2' else 0)
            assert all(t['event_id'] == event_id and t['status'] == 'submitted' for t in observed['tasks'])
            disk = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
            assert disk == operations
            return dict(mode=mode, case=case, passed=True, event=event, observed=observed, calls=calls,
                        client_ticks=dict(start_ns=started, admitted_ns=admitted, workflow_observed_ns=done),
                        ledger=str(ledger), ledger_sha256=file_hash(ledger), reports=str(reports), reports_sha256=file_hash(reports),
                        database=config['database'], operations=operations)
        finally:
            proc.terminate()
            try: proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.communicate(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.serve:
        serve(json.loads(args.serve.read_text(encoding='utf-8'))); return
    assert args.output and not args.output.exists()
    from run_issue_intake_pilot import jobs
    job = next(j for name, j, _ in jobs() if name == 'normal')
    example = next(e for e in json.loads((HERE / 'results/archive-development-examples-20260930-v3.json').read_text(encoding='utf-8'))['examples'] if e['adaptation']['target']['kind'] == 'issue')
    paths = [Path(__file__), HERE / 'run_issue_intake_pilot.py', HERE / 'run_archive_workflow_pilot.py',
             *(ROOT / 'backend/app' / path for path in ('api/os/events.py', 'api/internal/e2ag_mcp.py', 'services/event_dispatcher.py',
                'services/event_router.py', 'services/event_normalizer.py', 'services/a2a_service.py', 'services/e2ag_tool_gateway.py'))]
    output = dict(status='running', source_sha256={str(p.relative_to(ROOT)): file_hash(p) for p in paths}, rows=[],
                  boundary='Socket qualification, no latency/throughput conclusions. Isolated route app, not full main-app middleware. Native task+optional grant; fixed parent runner, upstream thread; not autonomous Agent or native task terminal transition.')
    for mode in ('G0', 'G1', 'G2'):
        for case in ('normal', 'other_read_object', 'admission_denied'):
            row = probe(mode, case, job, example)
            output['rows'].append(row)
            args.output.write_text(json.dumps(output, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(dict(mode=mode, case=case, passed=row['passed'])), flush=True)
    output['status'] = 'socket_paths_qualified_not_performance_results'
    args.output.write_text(json.dumps(output, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    main()
