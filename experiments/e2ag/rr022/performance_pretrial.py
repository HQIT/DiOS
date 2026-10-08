"""Local, development-only timing pretrial. Never a formal performance result.

Three processes: deterministic closed-loop driver, native route app, HTTP tool.
No holdout input, model, external write, policy change, or production deployment.
"""
import argparse
import asyncio
import base64
from collections import Counter
import contextlib
from contextvars import ContextVar
import copy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from run_paired_tool_pilot import ROOT, HERE, file_hash
from storage_runtime import pool_options

REQUEST = ContextVar('perf_request', default='outside_measurement')


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')


def percentile(values, probability):
    """Nearest-rank descriptive quantile; no interpolation or independence claim."""
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * probability) - 1)]


def stop_reason(free_disk, available_fraction, errors, low_memory_since, high_error_since, now):
    low_memory_since = (now if low_memory_since is None else low_memory_since) if available_fraction < .15 else None
    high_error_since = (now if high_error_since is None else high_error_since) if errors > .05 else None
    reason = 'disk_below_5GB' if free_disk < 5_000_000_000 else None
    if low_memory_since is not None and now - low_memory_since >= 10:
        reason = 'memory_below_15pct_for_10s'
    if high_error_since is not None and now - high_error_since >= 10:
        reason = 'error_rate_above_5pct_for_10s'
    return reason, low_memory_since, high_error_since


def serve_tool(config):
    directory = Path(config['directory'])
    ledger, reports = directory / 'operations.jsonl', directory / 'reports.jsonl'
    ledger.touch(exist_ok=False); reports.touch(exist_ok=False)
    lock, sequence = threading.Lock(), 0
    def append(path, value):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=True) + '\n')
            stream.flush(); os.fsync(stream.fileno())
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def send(self, status, value):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        def do_GET(self): self.send(200, {'ready': True, 'pid': os.getpid()})
        def do_POST(self):
            nonlocal sequence
            started = time.perf_counter_ns()
            try:
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert self.path == '/mcp' and req['method'] == 'tools/call'
                tool, args = req['params']['name'], req['params']['arguments']
                op = dict(rpc_id=req['id'], tool=tool, start_ns=started, client_port=self.client_address[1])
                if tool == 'snapshot.read':
                    item = next(x for x in config['job']['catalog'] if x['resource'] == args['resource'] and x['version'] == args['version'])
                    raw = Path(item['cache_path']).read_bytes()
                    assert hashlib.sha256(raw).hexdigest() == item['content_sha256']
                    value = {'content_base64': base64.b64encode(raw).decode()}
                    op.update(bytes=len(raw), content_sha256=item['content_sha256'])
                elif tool == 'report.write':
                    value = {'written': True}
                else: raise ValueError('Unknown tool')
                with lock:
                    if tool == 'report.write': append(reports, dict(rpc_id=req['id'], arguments=args))
                    op.update(sequence=sequence, persistence_start_ns=time.perf_counter_ns())
                    append(ledger, op); sequence += 1
                self.send(200, {'jsonrpc': '2.0', 'id': req['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps(value)}]}})
            except Exception as exc:
                self.send(500, {'error': type(exc).__name__ + ':' + str(exc)})
    server = ThreadingHTTPServer(('127.0.0.1', config['tool_port']), Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=.1)


def serve_app(config):
    sys.path.insert(0, str(ROOT / 'backend'))
    from fastapi import FastAPI
    from sqlalchemy import event as sql_event, text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    import uvicorn
    from app.config import settings
    from app.db.database import Base, get_db
    from app.models.tables import Agent, McpServer, Subscription
    from app.services import a2a_service, event_dispatcher
    from app.services.e2ag_tool_gateway import issue_task_mcp_config
    from app.services.e2ag_http_transport import mcp_transport_lifespan
    from app.api.os import events
    from app.api.internal import e2ag_mcp as proxy_module
    proxy_router = proxy_module.router
    settings.e2ag_mode = 'off' if config['mode'] == 'G0' else 'enforce'
    settings.event_dedup_enabled = False
    settings.webhook_secrets = {'github': config['test_secret']}
    settings.e2ag_internal_base_url = config['app_url']
    spans, ready, database = [], {}, {}
    request_db = ContextVar('perf_request_db', default=None)
    pool_activity = dict(checked_out=0, peak_checked_out=0)
    def span(stage, start, **extra):
        spans.append(dict(request_id=REQUEST.get(), stage=stage, start_ns=start, end_ns=time.perf_counter_ns(), **extra))
    class TimedSession(AsyncSession):
        async def commit(self):
            started = time.perf_counter_ns()
            try: return await super().commit()
            finally: span('db_commit', started)
    engine = create_async_engine('sqlite+aiosqlite:///' + config['database'].replace('\\', '/'),
                                 **pool_options(config.get('db_admission', 'parallel')), connect_args={'timeout': 5})
    @sql_event.listens_for(engine.sync_engine, 'checkout')
    def checkout(*_):
        pool_activity['checked_out'] += 1
        pool_activity['peak_checked_out'] = max(pool_activity['peak_checked_out'], pool_activity['checked_out'])
    @sql_event.listens_for(engine.sync_engine, 'checkin')
    def checkin(*_):
        pool_activity['checked_out'] -= 1
    @sql_event.listens_for(engine.sync_engine, 'connect')
    def configure_sqlite(connection, _):
        cursor = connection.cursor()
        cursor.execute('PRAGMA synchronous=FULL')
        cursor.execute('PRAGMA busy_timeout=5000')
        cursor.close()
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=TimedSession)
    def timed(function, name):
        def call(*args, **kwargs):
            started = time.perf_counter_ns()
            try: return function(*args, **kwargs)
            finally: span(name, started)
        return call
    def timed_async(function, name):
        async def call(*args, **kwargs):
            started = time.perf_counter_ns()
            try: return await function(*args, **kwargs)
            finally: span(name, started)
        return call
    NativeClient = proxy_module.httpx.AsyncClient
    class TimedProxyClient(NativeClient):
        def __init__(self, *args, **kwargs):
            started = time.perf_counter_ns()
            try: super().__init__(*args, **kwargs)
            finally: span('proxy_client_init', started)
        async def post(self, *args, **kwargs):
            started = time.perf_counter_ns()
            db = request_db.get()
            transaction_open = db.in_transaction() if db is not None else None
            try: return await super().post(*args, **kwargs)
            finally: span('proxy_upstream_post', started, db_transaction_open_at_upstream=transaction_open)
        async def __aexit__(self, *args):
            started = time.perf_counter_ns()
            try: return await super().__aexit__(*args)
            finally: span('proxy_client_close', started)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for key in ('journal_mode', 'synchronous', 'busy_timeout', 'page_size'):
                database[key] = (await connection.execute(text('PRAGMA ' + key))).scalar()
        async with sessions() as db:
            job = config['job']
            db.add(Agent(id='perf-agent', name='Isolated deterministic runner', mode='task', capabilities={'governance': {
                'allowed_event_sources': [job['allowed_source']], 'allowed_event_types': [job['allowed_type']],
                'allowed_actions': ['report.create'], 'require_action_declaration': True,
                'allowed_tools': ['snapshot.read', 'report.write'], 'argument_constraints': job['constraints']}}))
            db.add(McpServer(id='perf-tool', name='receipt', transport='streamable_http', url=config['upstream_url']))
            db.add(Subscription(agent_id='perf-agent', source_pattern=job['allowed_source'], event_types=[job['allowed_type']], enabled=True))
            await db.commit()
        async def fixed_send(**kwargs):
            started = time.perf_counter_ns()
            task = await a2a_service.create_task(**kwargs)
            span('native_task_create', started, event_id=task.context_id, task_id=task.id)
            if config['mode'] == 'G2':
                started = time.perf_counter_ns()
                db = kwargs['db']
                agent, server = await db.get(Agent, task.agent_id), await db.get(McpServer, 'perf-tool')
                connections, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
                assert not omissions
                connection = connections['receipt']
                span('grant_ready', started, event_id=task.context_id, task_id=task.id)
            else: connection = {'url': config['upstream_url'], 'headers': {}}
            ready[task.context_id] = dict(task_id=task.id, connection=connection)
            return task
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(a2a_service, 'send_message', fixed_send))
            stack.enter_context(patch.object(proxy_module.httpx, 'AsyncClient', TimedProxyClient))
            stack.enter_context(patch.object(proxy_module, 'validate_grant', timed_async(proxy_module.validate_grant, 'proxy_validate_grant')))
            stack.enter_context(patch.object(proxy_module, 'authorize_mcp_request', timed(proxy_module.authorize_mcp_request, 'proxy_authorize')))
            if config.get('persistence_fault'):
                from sqlalchemy.exc import OperationalError
                fault = config['persistence_fault']
                assert fault in {'intent_commit', 'read_result_commit', 'write_result_commit'}
                function_name = 'begin_tool_invocation' if fault == 'intent_commit' else 'finish_tool_invocation'
                original = getattr(proxy_module, function_name)
                async def injected(db, *args, **kwargs):
                    selected = fault == 'intent_commit' or kwargs.get('tool_name') == (
                        'snapshot.read' if fault == 'read_result_commit' else 'report.write')
                    if not selected:
                        return await original(db, *args, **kwargs)
                    async def fail_commit():
                        raise OperationalError('injected_commit', {}, Exception('bounded test fault'))
                    with patch.object(db, 'commit', fail_commit):
                        return await original(db, *args, **kwargs)
                stack.enter_context(patch.object(proxy_module, function_name, injected))
            for module, name, stage in [(events, 'detect_and_normalize', 'normalization'), (events, 'match_subscriptions', 'subscription_matching'),
                                        (event_dispatcher, 'evaluate_contract', 'contract_pdp'), (event_dispatcher, 'evaluate_policy', 'target_pdp')]:
                stack.enter_context(patch.object(module, name, timed(getattr(module, name), stage)))
            transport_started = time.perf_counter_ns()
            async with mcp_transport_lifespan(app, enabled=config.get('proxy_transport') == 'shared' and config['mode'] == 'G2'):
                span('proxy_transport_startup', transport_started)
                yield
        await engine.dispose()
    app = FastAPI(lifespan=lifespan)
    async def dependency():
        async with sessions() as db:
            token = request_db.set(db)
            try: yield db
            finally: request_db.reset(token)
    app.dependency_overrides[get_db] = dependency
    app.include_router(events.router, prefix='/api/os')
    app.include_router(proxy_router, prefix='/api')
    @app.middleware('http')
    async def timing(request, call_next):
        token = REQUEST.set(request.headers.get('x-perf-id', 'outside_measurement'))
        started = time.perf_counter_ns()
        try:
            response = await call_next(request)
            span('http_route', started, path=request.url.path, status=response.status_code)
            return response
        finally: REQUEST.reset(token)
    @app.get('/bench/ready')
    async def health(): return {'ready': True, 'database': database, 'pid': os.getpid()}
    @app.get('/bench/event/{event_id}')
    async def event_connection(event_id: str): return ready.pop(event_id, None)
    @app.post('/bench/finalize')
    async def finalize():
        dump(Path(config['directory']) / 'server-spans.json', spans)
        return dict(span_count=len(spans), database=database, pool_activity=pool_activity,
                    pool_options=pool_options(config.get('db_admission','parallel')))
    uvicorn.run(app, host='127.0.0.1', port=config['app_port'], log_level='error', access_log=False)


def development_input(kind):
    from run_issue_intake_pilot import jobs as issue_jobs
    from run_pr_manifest_pilot import jobs as pr_jobs
    if kind == 'issue': job = next(j for name, j, _ in issue_jobs() if name == 'normal')
    else:
        acquisition = json.loads((HERE / 'results/pr-manifest-snapshot-20260930-v1.json').read_text(encoding='utf-8'))
        job = next(j for name, j, _ in pr_jobs(acquisition['snapshot']) if name == 'normal')
    examples = json.loads((HERE / 'results/archive-development-examples-20260930-v3.json').read_text(encoding='utf-8'))['examples']
    example = next(e for e in examples if e['adaptation']['target']['kind'] == kind)
    for entry in job['catalog']: assert file_hash(Path(entry['cache_path'])) == entry['content_sha256']
    return job, example


def ports():
    with socket.socket() as left, socket.socket() as right:
        left.bind(('127.0.0.1', 0)); right.bind(('127.0.0.1', 0))
        return left.getsockname()[1], right.getsockname()[1]


def stop_process_tree(process):
    """Windows venv launchers may own a separate Python worker process."""
    import psutil
    try:
        parent = psutil.Process(process.pid)
        targets = list(reversed(parent.children(recursive=True))) + [parent]
    except psutil.NoSuchProcess:
        process.wait(timeout=5)
        return []
    for target in targets:
        try: target.terminate()
        except psutil.NoSuchProcess: pass
    _, alive = psutil.wait_procs(targets, timeout=5)
    for target in alive:
        try: target.kill()
        except psutil.NoSuchProcess: pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    process.wait(timeout=5)
    return [target.pid for target in alive]


async def drive(config, example, concurrency, warm, duration, pids):
    import httpx
    import psutil
    from run_issue_intake_pilot import report_arguments as issue_report
    from pr_manifest_workload import report_arguments as pr_report
    transform = issue_report if config['kind'] == 'issue' else pr_report
    records, resources, windows = [], [], []
    halt, finished = asyncio.Event(), asyncio.Event()
    stop = dict(reason=None)
    processes = [psutil.Process(pid) for pid in [os.getpid(), *pids]]
    for process in processes: process.cpu_percent()
    payload = copy.deepcopy(example['adaptation']['projected_payload'])
    payload.update(requested_action='report.create', requested_tool='snapshot.read', environment='test')
    raw = json.dumps(payload).encode()
    signature = hmac.new(config['test_secret'].encode(), raw, hashlib.sha256).hexdigest()
    headers = {'content-type': 'application/json', 'x-github-event': example['adaptation']['normalizer_event_name'], 'x-hub-signature-256': 'sha256=' + signature}
    async def monitor():
        low, high = None, None
        while not finished.is_set():
            now = time.monotonic()
            memory = psutil.virtual_memory()
            recent = [r for r in records if now - r['end_monotonic'] <= 10]
            rate = sum(bool(r.get('infrastructure_error')) for r in recent) / len(recent) if recent else 0
            free = shutil.disk_usage(ROOT).free
            reason, low, high = stop_reason(free, memory.available / memory.total, rate, low, high, now)
            resources.append(dict(monotonic=now, memory_available=memory.available, memory_total=memory.total, disk_free=free,
                recent_infrastructure_error_rate=rate, processes=[dict(pid=p.pid, cpu_percent_one_core=p.cpu_percent(), rss_bytes=p.memory_info().rss) for p in processes]))
            if reason: stop['reason'] = reason; halt.set()
            try: await asyncio.wait_for(finished.wait(), timeout=.5)
            except asyncio.TimeoutError: pass
    async with httpx.AsyncClient(trust_env=False, timeout=30, limits=httpx.Limits(max_connections=concurrency * 3, max_keepalive_connections=concurrency * 3)) as client:
        async def phase(name, seconds):
            window_start = time.perf_counter_ns()
            deadline = time.monotonic() + seconds
            async def worker(worker_id):
                counter = 0
                while not halt.is_set() and (counter < config['request_limit'] if config.get('request_limit') is not None else time.monotonic() < deadline):
                    counter += 1
                    rid = f'{name}-{worker_id}-{counter}'
                    planned = time.perf_counter_ns()
                    row = dict(request_id=rid, phase=name, worker=worker_id, planned_ns=planned, calls=[], success=False, infrastructure_error=False)
                    try:
                        started = time.perf_counter_ns()
                        response = await client.post(config['app_url'] + '/api/os/events/webhook/github', content=raw, headers={**headers, 'x-perf-id': rid + ':admission'})
                        row.update(send_ns=started, admitted_ns=time.perf_counter_ns(), admission_status=response.status_code)
                        response.raise_for_status()
                        event = response.json()
                        row.update(event_id=event['event_id'], trace_id=event['trace_id'], dispatch_status=event['status'])
                        if event.get('error') or event['status'] != 'dispatched': raise RuntimeError('Dispatch failed: ' + str(event.get('error')))
                        ready_started = time.perf_counter_ns()
                        ready = await client.get(config['app_url'] + '/bench/event/' + event['event_id'], headers={'x-perf-id': rid + ':ready'})
                        ready.raise_for_status()
                        row['connection_fetch_ns'] = time.perf_counter_ns() - ready_started
                        conn = ready.json()['connection']
                        args = config['job']['read_arguments']
                        for tool in ('snapshot.read', 'report.write'):
                            short = 'read' if tool == 'snapshot.read' else 'write'
                            call_start = time.perf_counter_ns()
                            result = await client.post(conn['url'], headers={**conn['headers'], 'x-perf-id': rid + ':' + short}, json={
                                'jsonrpc': '2.0', 'id': rid + ':' + short, 'method': 'tools/call', 'params': {'name': tool, 'arguments': args}})
                            call = dict(tool=tool, start_ns=call_start, end_ns=time.perf_counter_ns(), status=result.status_code,
                                        invocation_id=result.headers.get('x-e2ag-invocation-id'))
                            if result.status_code >= 400:
                                try:
                                    data = result.json().get('error', {}).get('data', {})
                                    call['error_data'] = {k:data[k] for k in ('reason', 'invocation_id', 'execution_outcome', 'forwarded', 'automatic_retry') if k in data}
                                except (ValueError, AttributeError, TypeError):
                                    pass
                            row['calls'].append(call)
                            result.raise_for_status()
                            content = json.loads(result.json()['result']['content'][0]['text'])
                            if tool == 'snapshot.read':
                                transformed = time.perf_counter_ns()
                                args = await asyncio.to_thread(transform, config['job'], content)
                                row['business_transform_ns'] = time.perf_counter_ns() - transformed
                            else: assert content['written'] is True
                        row['success'] = True
                    except Exception as exc:
                        row['error'] = type(exc).__name__ + ': ' + str(exc)
                        statuses = [row.get('admission_status', 0), *[c['status'] for c in row['calls']]]
                        row['infrastructure_error'] = isinstance(exc, (httpx.TransportError, RuntimeError)) or any(s >= 500 for s in statuses)
                        if not row['infrastructure_error']:
                            stop['reason'] = 'unexpected_semantic_or_measurement_failure'; halt.set()
                    finally:
                        row.update(end_ns=time.perf_counter_ns(), end_monotonic=time.monotonic())
                        records.append(row)
                    if len(records) >= config.get('request_budget', 10000):
                        stop['reason'] = 'pretrial_request_budget'; halt.set()
            await asyncio.gather(*(worker(n) for n in range(concurrency)))
            windows.append(dict(phase=name, start_ns=window_start, configured_seconds=seconds, drain_end_ns=time.perf_counter_ns()))
        monitoring = asyncio.create_task(monitor())
        try:
            if warm: await phase('warmup', warm)
            if not halt.is_set(): await phase('measurement', duration)
        finally:
            finished.set(); await monitoring
    return dict(records=records, resources=resources, windows=windows, stop_reason=stop['reason'])


def verify_cell(config, raw):
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.services.e2ag import verify_audit_chain
    directory = Path(config['directory'])
    operations = [json.loads(x) for x in (directory / 'operations.jsonl').read_text(encoding='utf-8').splitlines()]
    reports = [json.loads(x) for x in (directory / 'reports.jsonl').read_text(encoding='utf-8').splitlines()]
    spans = json.loads((directory / 'server-spans.json').read_text(encoding='utf-8'))
    span_keys = {s['request_id'] for s in spans}
    counts = Counter(x['rpc_id'] for x in operations)
    failures = []
    if config.get('db_admission') == 'queued':
        for span in spans:
            if span['stage'] == 'proxy_upstream_post' and span.get('db_transaction_open_at_upstream') is not False:
                failures.append('transaction_held_during_tool:' + span['request_id'])
    with sqlite3.connect(config['database']) as db:
        events = {e[0]: (e[1], json.loads(e[2])) for e in db.execute('select id, trace_id, audit_chain from event_logs')}
        tasks = list(db.execute('select id, context_id, trace_id, status from a2a_tasks'))
        grants = list(db.execute('select task_id, call_count from e2ag_tool_grants'))
        invocations = list(db.execute('select id, event_log_id, state from e2ag_tool_invocations'))
        for rid, row in ((r['request_id'], r) for r in raw['records']):
            if not row['success']: continue
            event = events[row['event_id']]
            task = [t for t in tasks if t[1] == row['event_id']]
            good = (len(task) == 1 and task[0][2] == row['trace_id'] == event[0] and task[0][3] == 'submitted'
                    and verify_audit_chain(event[1]) and counts[rid + ':read'] == counts[rid + ':write'] == 1
                    and rid + ':admission' in span_keys and rid + ':ready' in span_keys)
            if config['mode'] == 'G2':
                good &= ([g[1] for g in grants if g[0] == task[0][0]] == [2] if task else False)
                good &= sum(e['stage'] == 'tool_call' for e in event[1]) == 2
                intents = [e for e in event[1] if e['stage'] == 'tool_intent']
                calls = [e for e in event[1] if e['stage'] == 'tool_call']
                stored = [i for i in invocations if i[1] == row['event_id']]
                good &= len(intents) == len(stored) == 2 and all(i[2] == 'response_received' for i in stored)
                good &= {i[0] for i in stored} == {e['evidence'].get('invocation_id') for e in intents} == {e['evidence'].get('invocation_id') for e in calls}
                good &= rid + ':read' in span_keys and rid + ':write' in span_keys
            if not good: failures.append(rid)
        if config['mode'] != 'G2' and (grants or invocations): failures.append('unexpected_grants_or_invocations')
    # Independent business oracle from previous, separately checked development evidence.
    if config['kind'] == 'issue':
        issue = json.loads(Path(config['job']['catalog'][0]['cache_path']).read_bytes())
        title, body = issue['title'], issue['body'] or ''
        expected = dict(title_present=bool(title.strip()), title_characters=sum(1 for _ in title), body_characters=sum(1 for _ in body),
                        labels=sorted(set(x['name'] for x in issue['labels'])), queue='awaiting_triage' if title.strip() and body.strip() else 'needs_information')
    else:
        from check_pr_manifest_pilot import independent_inventory
        expected = independent_inventory(json.loads(Path(config['job']['catalog'][0]['cache_path']).read_bytes()))
    for report in reports:
        if json.loads(report['arguments']['report']) != expected: failures.append('report:' + report['rpc_id'])
    return dict(verified=not failures, failures=failures, events=len(events), tasks=len(tasks), grants=len(grants),
                reads=sum(o['tool'] == 'snapshot.read' for o in operations), writes=len(reports),
                invocation_states=dict(Counter(i[2] for i in invocations)),
                database_bytes=Path(config['database']).stat().st_size, server_span_count=len(spans),
                sql_commit_spans=sum(s['stage'] == 'db_commit' for s in spans),
                evidence_sha256={p.name: file_hash(p) for p in (directory / 'operations.jsonl', directory / 'reports.jsonl', directory / 'server-spans.json')})


def summarize(raw):
    rows = [r for r in raw['records'] if r['phase'] == 'measurement']
    good = [r for r in rows if r['success']]
    windows = [w for w in raw['windows'] if w['phase'] == 'measurement']
    def stats(values): return dict(n=len(values), p50_ms=percentile(values, .5), p95_ms=percentile(values, .95))
    summary = dict(measured_attempts=len(rows), completed=len(good), errors=len(rows)-len(good),
                   latency_population='successful complete workflows only; failures retained separately',
                   errors_by_type=dict(Counter(r.get('error','').split(':',1)[0] for r in rows if not r['success'])),
                   warmup_attempts=sum(r['phase'] == 'warmup' for r in raw['records']),
                   admission=stats([(r['admitted_ns']-r['send_ns'])/1e6 for r in good]),
                   workflow=stats([(r['end_ns']-r['planned_ns'])/1e6 for r in good]),
                   read=stats([(c['end_ns']-c['start_ns'])/1e6 for r in good for c in r['calls'] if c['tool']=='snapshot.read']),
                   write=stats([(c['end_ns']-c['start_ns'])/1e6 for r in good for c in r['calls'] if c['tool']=='report.write']))
    if windows:
        w = windows[0]; boundary = w['start_ns'] + int(w['configured_seconds'] * 1e9)
        summary.update(completed_inside_window=sum(r['end_ns'] <= boundary for r in good),
                       completion_rate_inside_window=sum(r['end_ns'] <= boundary for r in good) / w['configured_seconds'],
                       drain_seconds=(w['drain_end_ns']-boundary)/1e9,
                       actual_launch_and_drain_seconds=(w['drain_end_ns']-w['start_ns'])/1e9)
    return summary


def run_cell(kind, mode, concurrency, warm, duration, persistence_fault=None, proxy_transport='per-call', db_admission='parallel', request_limit=None):
    import httpx
    # Count-bounded fault qualification is independent of scheduler timing.
    # Timed performance profiles never set this option.
    if request_limit is not None:
        assert request_limit == 1 and concurrency == 1 and warm == 0
    job, example = development_input(kind)
    directory = Path(tempfile.mkdtemp(prefix='rr022-perf-pretrial-', dir=ROOT / 'tmp'))
    app_port, tool_port = ports()
    config = dict(kind=kind, mode=mode, concurrency=concurrency, job=job, directory=str(directory), database=str(directory/'state.db'),
                  app_port=app_port, tool_port=tool_port, app_url=f'http://127.0.0.1:{app_port}', upstream_url=f'http://127.0.0.1:{tool_port}/mcp',
                  test_secret='isolated-performance-only', persistence_fault=persistence_fault, proxy_transport=proxy_transport, db_admission=db_admission, request_limit=request_limit)
    config_path = directory/'config.json'; dump(config_path, config)
    processes, logs, service_pids = [], [], []
    result = dict(kind=kind, mode=mode, concurrency=concurrency, directory=str(directory), status='starting', proxy_transport=proxy_transport, db_admission=db_admission)
    try:
        for role, url in [('tool', config['upstream_url']), ('app', config['app_url']+'/bench/ready')]:
            log = (directory/(role+'.log')).open('w', encoding='utf-8'); logs.append(log)
            p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve-'+role, str(config_path)], cwd=ROOT,
                                 stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            processes.append(p)
            deadline = time.monotonic()+40
            with httpx.Client(trust_env=False, timeout=3) as client:
                while True:
                    try:
                        response = client.get(url)
                        if response.status_code == 200:
                            import psutil
                            service_pid = response.json()['pid']
                            descendants = psutil.Process(p.pid).children(recursive=True)
                            assert service_pid in {p.pid, *(child.pid for child in descendants)}, 'Readiness PID is not an owned child'
                            service_pids.append(service_pid)
                            if role == 'app': result['database_settings'] = response.json()['database']
                            break
                    except httpx.TransportError: pass
                    if p.poll() is not None or time.monotonic() > deadline: raise RuntimeError(role+' startup failed; inspect '+str(directory/(role+'.log')))
                    time.sleep(.1)
        result['process_identity'] = [dict(role=role, launcher_pid=p.pid, service_pid=pid) for role,p,pid in zip(('upstream','app'),processes,service_pids)]
        raw = asyncio.run(drive(config, example, concurrency, warm, duration, service_pids))
        dump(directory/'client-raw.json', raw)
        with httpx.Client(trust_env=False, timeout=30) as client:
            finalized = client.post(config['app_url']+'/bench/finalize')
            finalized.raise_for_status()
            result['storage_runtime'] = finalized.json()
            if db_admission == 'queued':
                assert result['storage_runtime']['pool_activity'] == {'checked_out':0, 'peak_checked_out':1}
        result.update(summary=summarize(raw), stop_reason=raw['stop_reason'], verification=verify_cell(config, raw),
                      raw_sha256=file_hash(directory/'client-raw.json'), payload_bytes=Path(job['catalog'][0]['cache_path']).stat().st_size)
        result['status'] = 'pretrial_checked' if result['verification']['verified'] and not raw['stop_reason'] else 'pretrial_failed_or_stopped'
    except Exception as exc:
        result.update(status='failed', error=type(exc).__name__+': '+str(exc))
    finally:
        for p in reversed(processes):
            alive = stop_process_tree(p)
            if alive: result.update(status='failed', cleanup_remaining_pids=alive)
        for log in logs: log.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve-app', type=Path)
    parser.add_argument('--serve-tool', type=Path)
    parser.add_argument('--profile', choices=['qualification', 'pretrial', 'process-check'], default='qualification')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--proxy-transport', choices=['per-call', 'shared'], default='per-call')
    parser.add_argument('--db-admission', choices=['parallel', 'queued'], default='parallel')
    args = parser.parse_args()
    if args.serve_app: return serve_app(json.loads(args.serve_app.read_text(encoding='utf-8')))
    if args.serve_tool: return serve_tool(json.loads(args.serve_tool.read_text(encoding='utf-8')))
    assert args.output and not args.output.exists()
    import psutil
    concurrency = [1] if args.profile != 'pretrial' else [1, 8, 32]
    warm, duration = (2, 8) if args.profile == 'pretrial' else (0, .5 if args.profile == 'process-check' else 3)
    randomizer = random.Random(20260930)
    order = []
    for kind in ('issue','pr'):
        for n in concurrency:
            modes = ['G0','G1','G2']; randomizer.shuffle(modes)
            order.extend((kind, m, n) for m in modes)
    if args.profile == 'process-check': order = [('issue','G2',1)]
    env = dict(platform=platform.platform(), processor=platform.processor(), python=sys.version,
               cpu_logical=psutil.cpu_count(), cpu_physical=psutil.cpu_count(logical=False), memory_total=psutil.virtual_memory().total,
               sqlite=sqlite3.sqlite_version, dependencies={p:importlib.metadata.version(p) for p in ['httpx','fastapi','uvicorn','SQLAlchemy','aiosqlite','psutil']})
    source_paths = [Path(__file__), HERE/'storage_runtime.py', *(ROOT/'backend/app'/s for s in ['api/os/events.py','api/internal/e2ag_mcp.py','services/e2ag.py','services/e2ag_arguments.py',
        'services/e2ag_tool_gateway.py','services/e2ag_http_transport.py','services/event_dispatcher.py','services/a2a_service.py', 'models/tables.py', 'db/database.py', 'config.py']), ROOT/'backend/main.py', HERE/'run_issue_intake_pilot.py', HERE/'pr_manifest_workload.py']
    output = dict(status='running', profile=args.profile, warmup_seconds=warm, measurement_seconds=duration, rounds=1, random_seed=20260930,
                  planned_order=order, environment=env, proxy_transport=args.proxy_transport, db_admission=args.db_admission,
                  source_sha256={str(p.relative_to(ROOT)):file_hash(p) for p in source_paths}, rows=[],
                  boundary='Development only. Closed-loop, three local processes, SQLite FULL/DELETE, one app worker. New DB/processes per cell, warmed DB retained. No model, holdout, autonomous lifecycle, p99, capacity, or external-engine speed claim.')
    dump(args.output, output)  # Protocol and order recorded before any request.
    started = time.monotonic()
    for kind, mode, n in order:
        if time.monotonic()-started > 1800 or shutil.disk_usage(ROOT).free < 5_000_000_000:
            output['stop_reason']='global_time_or_disk_budget'; break
        row = run_cell(kind, mode, n, warm, duration, proxy_transport=args.proxy_transport, db_admission=args.db_admission); output['rows'].append(row); dump(args.output, output)
        print(json.dumps({k:row.get(k) for k in ['kind','mode','concurrency','status','summary','error','stop_reason']}), flush=True)
        if row['status'] != 'pretrial_checked': break
    output['status'] = 'development_pretrial_complete' if len(output['rows'])==len(order) and all(r['status']=='pretrial_checked' for r in output['rows']) else 'development_pretrial_incomplete'
    output['elapsed_seconds']=time.monotonic()-started; dump(args.output, output)


if __name__ == '__main__':
    os.environ['PYTHONIOENCODING']='utf-8'
    main()
