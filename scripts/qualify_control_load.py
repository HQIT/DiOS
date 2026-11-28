"""Isolated k6 HTTP capacity baseline; no model, Worker or production endpoint."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests')]
import uvicorn
from sqlalchemy import func, select, text
from app.runtime.schema import attempts, runs
from test_runtime_principals import PrincipalTests

K6_IMAGE = 'grafana/k6@sha256:6a3ee54ac0e9ff5527923f6295257453dd88012f32f40dadf0eb1b638cbb21c7'


def values(summary, name):
    return summary.get('metrics', {}).get(name, {}).get('values', {})


def qualifies(report):
    # k6 upstream #3052 documents one boundary iteration at the duration cutoff.
    # Keep the actual count; never allow missing work, dropped work or errors.
    intended, completed = report['intended_iterations'], report['completed_iterations']
    count_ok = intended <= completed <= intended + (1 if report['arrival_rate'] else 0)
    return (report['k6_exit'] == 0 and report['semantic_errors'] == 0
        and report['accounting_consistent'] and report['operation_success_rate'] == 1
        and report['dropped_iterations'] == 0 and count_ok)


async def case(output, image, concurrency, rate, duration, iterations):
    output.mkdir(mode=0o700)
    fixture = PrincipalTests()
    await fixture.asyncSetUp()
    server_task = None
    container_name = 'dios-control-load-' + uuid4().hex[:12]
    listener = socket.socket()
    try:
        own = await fixture.submit_as('a', 'seed')
        other = await fixture.submit_as('b', 'seed')
        listener.bind(('127.0.0.1', 0))
        listener.listen(2048)
        server = uvicorn.Server(uvicorn.Config(fixture.app, log_level='error', access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done(): await server_task
                await asyncio.sleep(.02)
        client = output / 'client.json'
        client.write_text(json.dumps(dict(base=f'http://127.0.0.1:{listener.getsockname()[1]}/api/os/runtime',
            token=fixture.tokens['a'], own=own['id'], other=other['id'])))
        client.chmod(0o600)
        before = resource.getrusage(resource.RUSAGE_SELF)
        start = time.monotonic()
        command = ['docker', 'run', '--rm', '--name', container_name, '--network', 'host', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '-v', f'{client}:/input/client.json:ro',
            '-v', f'{ROOT / "scripts/fixtures/control_load.js"}:/input/load.js:ro',
            '-v', f'{output}:/output', '-e', f'CONCURRENCY={concurrency}',
            '-e', f'ARRIVAL_RATE={rate}', '-e', f'DURATION={duration}',
            '-e', f'ITERATIONS={iterations}', image, 'run', '--quiet', '/input/load.js']
        with (output / 'k6.log').open('w') as log:
            completed = await asyncio.to_thread(subprocess.run, command, stdout=log,
                stderr=subprocess.STDOUT, timeout=duration + 120, check=False)
        elapsed = time.monotonic() - start
        after = resource.getrusage(resource.RUSAGE_SELF)
        summary = json.loads((output / 'k6-summary.json').read_text())
        async with fixture.db.read(fixture.console.scope) as connection:
            run_count = (await connection.execute(select(func.count()).select_from(runs))).scalar_one()
            revoked = (await connection.execute(select(func.count()).select_from(runs).where(runs.c.revoked.is_(True)))).scalar_one()
            attempt_count = (await connection.execute(select(func.count()).select_from(attempts))).scalar_one()
            durability = {key: (await connection.execute(text('PRAGMA ' + key))).scalar_one()
                for key in ('journal_mode', 'synchronous')}
        cycles = int(values(summary, 'completed_write_cycles').get('count', 0))
        semantic_errors = int(values(summary, 'semantic_errors').get('count', 0))
        success = values(summary, 'operation_success').get('rate', 0)
        dropped = int(values(summary, 'dropped_iterations').get('count', 0))
        measured_iterations = int(values(summary, 'iterations').get('count', 0))
        intended = rate * duration if rate else iterations
        accounting = run_count == 2 + cycles and revoked == cycles and attempt_count == 0
        report = dict(concurrency_limit=concurrency, arrival_rate=rate,
            model='constant-arrival-rate' if rate else 'shared-iterations',
            intended_iterations=intended, completed_iterations=measured_iterations,
            dropped_iterations=dropped, k6_exit=completed.returncode,
            semantic_errors=semantic_errors, operation_success_rate=success,
            completed_write_cycles=cycles, run_count=run_count, revoked_count=revoked,
            attempt_count=attempt_count, accounting_consistent=accounting,
            http_duration_ms=values(summary, 'http_req_duration'),
            operation_duration_ms={name:values(summary, name+'_ms')
                for name in ('detail','list','deny','submit','replay','revoke')},
            http_requests=values(summary, 'http_reqs'), iteration_duration_ms=values(summary, 'iteration_duration'),
            seconds=round(elapsed, 3), durability=durability,
            writer_queue=fixture.db.write_gate.snapshot(),
            server_cpu_seconds=round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 3),
            runner_process_peak_rss_kib=after.ru_maxrss)
        report['passed'] = qualifies(report)
        (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        return report
    finally:
        await asyncio.to_thread(subprocess.run, ['docker','rm','-f',container_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
        if server_task:
            server.should_exit = True
            await asyncio.wait_for(server_task, 30)
        listener.close()
        await fixture.asyncTearDown()
        # The ephemeral token is no longer needed and must not be published.
        (output / 'client.json').unlink(missing_ok=True)


async def main(args):
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'workspace') or output.exists():
        raise ValueError('Use a new output directory under workspace')
    output.mkdir(parents=True, mode=0o700)
    image = subprocess.check_output(['docker', 'image', 'inspect', K6_IMAGE, '--format', '{{.Id}}'], text=True).strip()
    reports = []
    for repeat in range(args.repeats):
        for concurrency in args.concurrency:
            label = f'concurrency-{concurrency}-repeat-{repeat+1}'
            report = await case(output / label, image, concurrency, 0, args.duration, args.iterations)
            reports.append(dict(label=label, **report))
            print(json.dumps(dict(case=label, passed=report['passed'], seconds=report['seconds'])), flush=True)
        for rate in args.rates:
            label = f'arrival-{rate}-repeat-{repeat+1}'
            report = await case(output / label, image, 128, rate, args.duration, args.iterations)
            reports.append(dict(label=label, **report))
            print(json.dumps(dict(case=label, passed=report['passed'], seconds=report['seconds'])), flush=True)
    paths = ['scripts/qualify_control_load.py', 'scripts/fixtures/control_load.js',
        'backend/app/runtime/database.py', 'backend/app/services/task_console.py',
        'backend/app/api/os/runtime_tasks.py', 'backend/tests/test_runtime_principals.py']
    report = dict(work_item='D01-4.01', complete=False, passed=all(r['passed'] for r in reports),
        implementation_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        mode='local-http-control-plane', k6_image=image, k6_manifest=K6_IMAGE,
        platform=platform.platform(), logical_cpus=os.cpu_count(),
        cpu_affinity=len(os.sched_getaffinity(0)),
        dependencies={name:importlib.metadata.version(name) for name in ('SQLAlchemy','aiosqlite','uvicorn','fastapi')},
        source_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
        cases=reports, limits=[
            'Loopback HTTP, SQLite FULL with measured journal mode, local file identities, builtin E2AG Work admission/revocation.',
            'No Worker execution, model, TLS, tool dispatch, approval or delegation-chain load in this baseline.',
            'Fresh database per case with two seed tasks; no 90-day history, sustained soak, physical hosts or HA qualification.',
            'Load generator shares the host; VUs are clients, not independent Agent processes.',
            'RSS is the runner process lifetime high-water mark; CPU excludes k6 and infrastructure.',
            'Two-tier SLO, full E2AG mix and unit cost remain unqualified.'])
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(dict(output=str(output), passed=report['passed'], complete=False)), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, nargs='+', default=[1,8,32,128])
    parser.add_argument('--rates', type=int, nargs='*', default=[10,100])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--iterations', type=int, default=256)
    parser.add_argument('--duration', type=int, default=10)
    args = parser.parse_args()
    if any(n <= 0 for n in [*args.concurrency, *args.rates, args.repeats, args.iterations, args.duration]):
        parser.error('All sizes and durations must be positive')
    raise SystemExit(asyncio.run(main(args)))
