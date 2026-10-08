"""Isolated PostgreSQL timing harness. Production code is never modified.

Both configurations execute the same native proxy route and HTTP transport.
G0 substitutes only governance functions, within this benchmark process.
"""
import argparse
import asyncio
import contextlib
import copy
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from overhead_spans import Recorder, exclusive_rows
from run_paired_tool_pilot import ROOT
import performance_pretrial as legacy


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')


def serve_app(config):
    sys.path.insert(0, str(ROOT / 'backend'))
    from fastapi import FastAPI
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
    import uvicorn
    from app.config import settings
    from app.db.database import Base, get_db
    from app.models.tables import Agent, McpServer, Subscription
    from app.services import a2a_service, event_dispatcher, e2ag_tool_gateway as gateway
    from app.services.e2ag_http_transport import mcp_transport_lifespan
    from app.api.os import events
    from app.api.internal import e2ag_mcp as proxy

    if os.getenv('E2AG_ISOLATED_OVERHEAD') != '1':
        raise RuntimeError('This control configuration requires an isolated benchmark container')
    full = config['mode'] == 'G2'
    settings.e2ag_mode = 'enforce' if full else 'off'
    settings.event_dedup_enabled = False  # common setting: independent fresh task per arrival
    settings.webhook_secrets = {'github': config['test_secret']}
    settings.e2ag_internal_base_url = config['app_url']
    recorder = Recorder(enabled=config['instrument'])
    ready, controls, route_calls = {}, {}, []
    engine = create_async_engine(config['dsn'], pool_size=16, max_overflow=0, pool_timeout=30)

    class TimedSession(AsyncSession):
        async def commit(self):
            with recorder.span('db_commit'):
                return await super().commit()

    sessions = async_sessionmaker(engine, class_=TimedSession, expire_on_commit=False)
    original_create = a2a_service.create_task
    async def fixed_send(**kwargs):
        with recorder.span('task_create'):
            task = await original_create(**kwargs)
        db = kwargs['db']
        if full:
            with recorder.span('grant_issue'):
                agent, server = await db.get(Agent, task.agent_id), await db.get(McpServer, 'perf-tool')
                connections, omissions = await gateway.issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
                if omissions:
                    raise RuntimeError('grant omitted')
                connection = connections['receipt']
        else:
            # A routing handle is not a grant: no capability record or permission is created.
            handle = 'control-' + task.id
            controls[handle] = SimpleNamespace(mcp_server_id='perf-tool', allowed_tools=[], argument_constraints=None)
            connection = {'url': config['app_url'] + '/api/internal/e2ag/mcp/' + handle, 'headers': {}}
        ready[task.context_id] = {'task_id': task.id, 'connection': connection}
        return task

    async def control_validate(db, handle, token):
        return controls.get(handle), 'CONTROL_ROUTE'

    async def control_intent(db, routing, payload):
        # End the common server-lookup transaction before network I/O in both arms.
        await db.commit()
        return 'control-' + str(payload['id'])

    async def control_response(*args, **kwargs):
        return None

    async def control_audit(*args, **kwargs):
        return None

    NativeClient = proxy.httpx.AsyncClient
    class TimedClient(NativeClient):
        async def post(self, *args, **kwargs):
            with recorder.span('upstream_roundtrip'):
                return await super().post(*args, **kwargs)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            job = config['job']
            db.add(Agent(id='perf-agent', name='Isolated deterministic runner', mode='task', capabilities={'governance': {
                'allowed_event_sources': [job['allowed_source']], 'allowed_event_types': [job['allowed_type']],
                'allowed_actions': ['report.create'], 'require_action_declaration': True,
                'allowed_tools': ['snapshot.read', 'report.write'], 'argument_constraints': job['constraints']}}))
            db.add(McpServer(id='perf-tool', name='receipt', transport='streamable_http', url=config['upstream_url']))
            db.add(Subscription(agent_id='perf-agent', source_pattern=job['allowed_source'], event_types=[job['allowed_type']], enabled=True))
            await db.commit()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(a2a_service, 'send_message', fixed_send))
            if not full:
                for module, name, function in [
                    (proxy, 'validate_grant', control_validate),
                    (proxy, 'authorize_mcp_request', lambda *a: (True, 'CONTROL_ROUTE')),
                    (proxy, 'begin_tool_invocation', control_intent),
                    (proxy, 'finish_tool_invocation', control_response),
                    (event_dispatcher, 'append_audit_entry', lambda chain, **kw: chain),
                    (event_dispatcher, '_append_event_audit', control_audit),
                ]:
                    stack.enter_context(patch.object(module, name, function))
            targets = [
                (events, 'detect_and_normalize', 'normalization', False),
                (events, 'match_subscriptions', 'subscription_matching', False),
                (event_dispatcher, 'evaluate_contract', 'contract', False),
                (event_dispatcher, 'evaluate_policy', 'policy', False),
                (event_dispatcher, '_append_event_audit', 'dispatch_audit', True),
                (proxy, 'validate_grant', 'grant_check', True),
                (proxy, 'authorize_mcp_request', 'argument_check', False),
                (proxy, 'begin_tool_invocation', 'intent_commit', True),
                (proxy, 'finish_tool_invocation', 'response_commit', True),
            ]
            for module, name, stage, async_ in targets:
                stack.enter_context(patch.object(module, name, recorder.wrap(getattr(module, name), stage, async_)))
            stack.enter_context(patch.object(proxy.httpx, 'AsyncClient', TimedClient))
            async with mcp_transport_lifespan(app, enabled=True):
                yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    async def dependency():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = dependency
    app.include_router(events.router, prefix='/api/os')
    app.include_router(proxy.router, prefix='/api')

    @app.middleware('http')
    async def timing(request, call_next):
        rid = request.headers.get('x-perf-id', 'outside')
        token = recorder.request.set(rid)
        try:
            with recorder.span('http_route'):
                response = await call_next(request)
                if rid != 'outside':
                    route_calls.append({'request_id': rid, 'path': request.url.path, 'status': response.status_code})
                return response
        finally:
            recorder.request.reset(token)

    @app.get('/bench/ready')
    async def health():
        return {'ready': True, 'pid': os.getpid()}

    @app.get('/bench/event/{event_id}')
    async def connection(event_id: str):
        return ready.pop(event_id, None)

    @app.post('/bench/finalize')
    async def finalize():
        # Exclude this live administrative root; only completed measured requests.
        spans = [r for r in recorder.rows if r['request_id'] != 'outside']
        save(Path(config['directory']) / 'server-spans.json', exclusive_rows(spans))
        save(Path(config['directory']) / 'routes.json', route_calls)
        tables = {}
        async with sessions() as db:
            async with db.begin():
                for table in ['event_logs', 'a2a_tasks', 'e2ag_tool_grants', 'e2ag_tool_invocations']:
                    rows = await db.execute(text("SELECT (to_jsonb(t) - 'token_hash')::text FROM " + table + ' t'))
                    tables[table] = [json.loads(r[0]) for r in rows]
        save(Path(config['directory']) / 'database.json', tables)
        return {'exported': True, 'spans': len(spans), 'mode': config['mode']}

    uvicorn.run(app, host='127.0.0.1', port=config['app_port'], log_level='warning', access_log=False)


def verify(config, raw):
    from collections import Counter
    directory = Path(config['directory'])
    rows = lambda name: [json.loads(x) for x in (directory / name).read_text().splitlines()]
    operations, reports = rows('operations.jsonl'), rows('reports.jsonl')
    routes = json.loads((directory / 'routes.json').read_text())
    db = json.loads((directory / 'database.json').read_text())
    counts = Counter(op['rpc_id'] for op in operations)
    failures = []
    from check_formal_holdout import expected_report, report_matches
    wanted = expected_report(config['kind'], Path(config['job']['catalog'][0]['cache_path']).read_bytes())
    for report in reports:
        if not report_matches(report['arguments']['report'], wanted):
            failures.append('report_content:' + report['rpc_id'])
    for r in raw['records']:
        if r['success']:
            events = [e for e in db['event_logs'] if e['id'] == r['event_id']]
            tasks = [t for t in db['a2a_tasks'] if t['context_id'] == r['event_id']]
            if len(events) != 1 or len(tasks) != 1 or tasks[0]['trace_id'] != r['trace_id']:
                failures.append('task_event_binding:' + r['request_id'])
            if config['mode'] == 'G2' and tasks:
                grants = [g for g in db['e2ag_tool_grants'] if g['task_id'] == tasks[0]['id']]
                calls = [i for i in db['e2ag_tool_invocations'] if i['event_log_id'] == r['event_id']]
                if len(grants) != 1 or grants[0]['call_count'] != 2 or len(calls) != 2 or any(i['state'] != 'response_received' for i in calls):
                    failures.append('grant_invocation_binding:' + r['request_id'])
            for short in ['read', 'write']:
                rid = r['request_id'] + ':' + short
                if counts[rid] != 1:
                    failures.append('upstream_count:' + rid)
                matches = [v for v in routes if v['request_id'] == rid]
                if len(matches) != 1 or not matches[0]['path'].startswith('/api/internal/e2ag/mcp/'):
                    failures.append('proxy_route:' + rid)
    success = sum(r['success'] for r in raw['records'])
    if len(reports) != success:
        failures.append('report_count')
    if config['mode'] == 'G0' and (db['e2ag_tool_grants'] or db['e2ag_tool_invocations']):
        failures.append('control_has_governance_records')
    if config['mode'] == 'G2':
        if len(db['e2ag_tool_grants']) != len(raw['records']) or len(db['e2ag_tool_invocations']) != 2 * success:
            failures.append('full_missing_governance_records')
        sys.path.insert(0, str(ROOT / 'backend'))
        from app.services.e2ag import verify_audit_chain
        for e in db['event_logs']:
            if not verify_audit_chain(e['audit_chain']):
                failures.append('audit_chain')
    return {'passed': not failures, 'failures': failures, 'successes': success, 'content_checked': len(reports), 'events': len(db['event_logs']),
            'tasks': len(db['a2a_tasks']), 'grants': len(db['e2ag_tool_grants']), 'invocations': len(db['e2ag_tool_invocations']),
            'reads': sum(o['tool'] == 'snapshot.read' for o in operations), 'writes': len(reports)}


def run(config):
    import httpx
    import subprocess
    directory = Path(config['directory'])
    directory.mkdir(parents=True, exist_ok=False)
    config_path = directory / 'config.json'
    save(config_path, config)
    processes, logs, pids = [], [], []
    result = {'mode': config['mode'], 'kind': config['kind'], 'instrument': config['instrument'], 'directory': str(directory)}
    def cgroup():
        return {name: (Path('/sys/fs/cgroup')/name).read_text().strip()
                for name in ['cpu.max', 'cpu.stat', 'memory.max', 'memory.events'] if (Path('/sys/fs/cgroup')/name).exists()}
    result['cgroup_before'] = cgroup()
    try:
        for role, url in [('tool', config['upstream_url']), ('app', config['app_url'] + '/bench/ready')]:
            log = (directory / (role + '.log')).open('w'); logs.append(log)
            p = subprocess.Popen([sys.executable, __file__, '--serve-' + role, str(config_path)], stdout=log, stderr=subprocess.STDOUT)
            processes.append(p)
            deadline = time.monotonic() + 45
            while True:
                try:
                    response = httpx.get(url, timeout=2, trust_env=False)
                    if response.status_code == 200:
                        pid = response.json()['pid']
                        if pid != p.pid:
                            raise RuntimeError('Unexpected process identity')
                        pids.append(pid)
                        break
                except httpx.TransportError:
                    pass
                if p.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(role + ' startup failed')
                time.sleep(.1)
        raw = asyncio.run(legacy.drive(config, config['example'], config['concurrency'], config['warmup'], config['duration'], pids))
        save(directory / 'client-raw.json', raw)
        response = httpx.post(config['app_url'] + '/bench/finalize', timeout=60, trust_env=False)
        response.raise_for_status()
        result.update(summary=legacy.summarize(raw), verification=verify(config, raw), stop_reason=raw['stop_reason'])
    except Exception as exc:
        result.update(error=type(exc).__name__ + ': ' + str(exc))
    finally:
        for process in reversed(processes):
            legacy.stop_process_tree(process)
        for log in logs:
            log.close()
        result['cgroup_after'] = cgroup()
        save(directory / 'result.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve-app', type=Path)
    parser.add_argument('--serve-tool', type=Path)
    parser.add_argument('--run', type=Path)
    args = parser.parse_args()
    path = args.serve_app or args.serve_tool or args.run
    config = json.loads(path.read_text())
    if args.serve_app:
        serve_app(config)
    elif args.serve_tool:
        legacy.serve_tool(config)
    else:
        print(json.dumps(run(config)))
