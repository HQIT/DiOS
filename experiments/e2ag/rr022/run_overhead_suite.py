"""Run a predeclared suite in an isolated, unpublished Docker network."""
import argparse
import asyncio
import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import random
import sys
import time
import uuid
import asyncpg
from governance_overhead import run, save
from performance_pretrial import ports
from run_paired_tool_pilot import ROOT


async def create_database(name):
    assert name.startswith('overhead_') and name.replace('_', '').isalnum()
    connection = await asyncpg.connect(host='e2ag-overhead-pg-20261008', user='postgres', database='postgres')
    try:
        await connection.execute('CREATE DATABASE "' + name + '"')
    finally:
        await connection.close()
    return 'postgresql+asyncpg://postgres@e2ag-overhead-pg-20261008/' + name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', choices=['qualification', 'instrumentation', 'formal'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    schedule = []
    if args.profile == 'qualification':
        for kind in ['issue', 'pr']:
            for n in [1, 8]:
                for mode in ['G0', 'G2']:
                    schedule.append(dict(kind=kind, mode=mode, concurrency=n, instrument=True, request_limit=1,
                                         warmup=0, duration=1, case='normal'))
        for mode in ['G0', 'G2']:
            schedule.append(dict(kind='issue', mode=mode, concurrency=1, instrument=True, request_limit=1,
                                 warmup=0, duration=1, case='resource_mismatch'))
    else:
        rng = random.Random(20261008)
        for kind in ['issue', 'pr']:
            for n in ([1] if args.profile == 'instrumentation' else [1, 8]):
                for repeat in range(5):
                    arms = [False, True] if args.profile == 'instrumentation' else ['G0', 'G2']
                    rng.shuffle(arms)
                    for arm in arms:
                        schedule.append(dict(kind=kind, mode='G2' if isinstance(arm, bool) else arm,
                            concurrency=n, instrument=arm if isinstance(arm, bool) else True, request_limit=None,
                            warmup=5 if args.profile == 'instrumentation' else 30,
                            duration=15 if args.profile == 'instrumentation' else 180, repeat=repeat, case='normal'))
    manifest = {'profile': args.profile, 'schedule': schedule, 'platform': platform.platform(), 'python': sys.version,
                'dependencies': {name: importlib.metadata.version(name) for name in ['fastapi', 'sqlalchemy', 'httpx', 'asyncpg', 'psutil']},
                'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for folder in [ROOT/'backend/app', ROOT/'experiments/e2ag/rr022']
                                 for p in folder.rglob('*.py')}, 'started_unix': time.time()}
    save(args.output/'manifest.json', manifest)
    results = []
    for index, entry in enumerate(schedule):
        bundle = json.loads((ROOT/'inputs'/(entry['kind']+'-job.json')).read_text())
        config = {**entry, **copy.deepcopy(bundle)}
        if entry['case'] == 'resource_mismatch':
            config['job']['read_arguments']['resource'] += '-test-alias'
        app_port, tool_port = ports()
        dsn = asyncio.run(create_database('overhead_' + uuid.uuid4().hex))
        config.update(directory=str(args.output / f'cell-{index:02}'), dsn=dsn, request_budget=100000,
                      app_port=app_port, tool_port=tool_port, app_url=f'http://127.0.0.1:{app_port}',
                      upstream_url=f'http://127.0.0.1:{tool_port}/mcp', test_secret='isolated-performance-only')
        result = {**entry, **run(config)}
        expected_denial = entry['case'] == 'resource_mismatch' and entry['mode'] == 'G2'
        valid = result.get('verification', {}).get('passed', False)
        if expected_denial:
            valid = valid and result['verification']['reads'] == result['verification']['writes'] == 0
            raw = json.loads((Path(config['directory'])/'client-raw.json').read_text())
            valid = valid and all(r['calls'][0]['status'] == 403 for r in raw['records'])
        else:
            valid = valid and not result.get('stop_reason') and not result.get('summary', {}).get('errors', 1)
        result['qualification_expected'] = valid
        results.append(result)
        save(args.output/'results.json', {'rows': results, 'expected_cells': len(schedule), 'finished': False})
        print(json.dumps({k: result.get(k) for k in ['kind', 'mode', 'concurrency', 'instrument', 'case', 'qualification_expected', 'error', 'verification']}), flush=True)
        if not valid:
            break
    unchanged = all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest() == h for p,h in manifest['source_sha256'].items())
    save(args.output/'results.json', {'rows': results, 'expected_cells': len(schedule), 'finished': len(results)==len(schedule),
                                     'source_unchanged': unchanged, 'finished_unix': time.time()})


if __name__ == '__main__':
    main()
