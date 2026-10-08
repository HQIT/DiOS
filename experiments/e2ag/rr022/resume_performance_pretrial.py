"""Run only stopped/unrun cells, preserving the old short pretrial unchanged."""
import argparse
import json
import importlib.metadata
from pathlib import Path
import platform
import shutil
import sqlite3
import sys
import time

from performance_pretrial import ROOT, dump, file_hash, run_cell


def remaining_order(source):
    completed = {(r['kind'], r['mode'], r['concurrency']) for r in source['rows']
                 if r['status'] == 'pretrial_checked' and not r.get('stop_reason')
                 and r.get('verification', {}).get('verified')}
    return [cell for cell in source['planned_order'] if tuple(cell) not in completed]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    source = json.loads(args.input.read_text(encoding='utf-8'))
    assert source['status'] == 'development_pretrial_incomplete'
    assert source['profile'] == 'pretrial'
    for path, digest in source['source_sha256'].items():
        assert file_hash(ROOT / path) == digest, 'Source changed: ' + path
    import psutil
    memory = psutil.virtual_memory()
    assert memory.available / memory.total >= .15 and shutil.disk_usage(ROOT).free >= 5_000_000_000
    environment = dict(platform=platform.platform(), processor=platform.processor(), python=sys.version,
                       cpu_logical=psutil.cpu_count(), cpu_physical=psutil.cpu_count(logical=False), memory_total=memory.total,
                       sqlite=sqlite3.sqlite_version, dependencies={p: importlib.metadata.version(p) for p in source['environment']['dependencies']})
    assert environment == source['environment'], 'Execution environment changed; use a separately approved protocol'
    order = remaining_order(source)
    assert order
    result = {key: source[key] for key in ('profile', 'warmup_seconds', 'measurement_seconds', 'rounds',
              'random_seed', 'environment', 'proxy_transport', 'db_admission', 'boundary')}
    result.update(status='running', predecessor=str(args.input.relative_to(ROOT) if args.input.is_absolute() else args.input),
                  predecessor_sha256=file_hash(args.input), planned_order=order, rows=[],
                  source_sha256={**source['source_sha256'], str(Path(__file__).resolve().relative_to(ROOT)): file_hash(Path(__file__))},
                  continuation_boundary='Separate execution; never merge into a single uninterrupted performance round.',
                  preflight_available_fraction=memory.available / memory.total)
    dump(args.output, result)
    started = time.monotonic()
    for kind, mode, concurrency in order:
        if time.monotonic() - started > 1800 or shutil.disk_usage(ROOT).free < 5_000_000_000:
            result['stop_reason'] = 'global_time_or_disk_budget'
            break
        row = run_cell(kind, mode, concurrency, source['warmup_seconds'], source['measurement_seconds'],
                       proxy_transport=source['proxy_transport'], db_admission=source['db_admission'])
        result['rows'].append(row)
        dump(args.output, result)
        print(json.dumps({key: row.get(key) for key in ('kind', 'mode', 'concurrency', 'status', 'summary', 'stop_reason', 'error')}), flush=True)
        if row['status'] != 'pretrial_checked':
            break
    result['status'] = 'development_pretrial_complete' if len(result['rows']) == len(order) and all(r['status'] == 'pretrial_checked' for r in result['rows']) else 'development_pretrial_incomplete'
    result['elapsed_seconds'] = time.monotonic() - started
    dump(args.output, result)


if __name__ == '__main__':
    main()
