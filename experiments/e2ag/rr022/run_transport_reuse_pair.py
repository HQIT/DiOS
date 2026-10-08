"""Fixed, bounded transport intervention on the existing development issue."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import time

import psutil
from performance_pretrial import ROOT, dump, file_hash, run_cell


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    order = [(1, 'per-call'), (1, 'shared'), (8, 'shared'), (8, 'per-call')]
    sources = [Path(__file__), Path(__file__).with_name('performance_pretrial.py'),
               *(ROOT/'backend/app'/p for p in ('api/internal/e2ag_mcp.py', 'services/e2ag_tool_gateway.py',
                 'services/e2ag_http_transport.py', 'models/tables.py', 'config.py', 'services/e2ag_arguments.py'))]
    result = dict(status='running', created_at=datetime.now(timezone.utc).isoformat(), planned_order=order,
                  warmup_seconds=2, measurement_seconds=8, rounds=1,
                  environment=dict(platform=platform.platform(), memory_total=psutil.virtual_memory().total,
                                   cpu_logical=psutil.cpu_count(), dependencies={p:importlib.metadata.version(p) for p in ('httpx','SQLAlchemy','aiosqlite')}),
                  source_sha256={str(p.relative_to(ROOT)):file_hash(p) for p in sources}, rows=[],
                  boundary='Single-round G2 development issue intervention; per-call vs shared transport. Not formal performance; fixed alternating order, previous runs not pooled.')
    dump(args.output, result)
    started = time.monotonic()
    for concurrency, mode in order:
        memory = psutil.virtual_memory()
        if memory.available/memory.total < .15 or shutil.disk_usage(ROOT).free < 5_000_000_000 or time.monotonic()-started > 600:
            result['stop_reason'] = 'pair_resource_or_time_budget'
            break
        cell = run_cell('issue', 'G2', concurrency, 2, 8, proxy_transport=mode)
        result['rows'].append(cell)
        dump(args.output, result)
        print(json.dumps({k:cell.get(k) for k in ('proxy_transport','concurrency','status','summary','error','stop_reason')}), flush=True)
        if cell.get('stop_reason') and cell['stop_reason'] != 'error_rate_above_5pct_for_10s':
            break
        if cell['status'] == 'failed':
            break
    result['status'] = 'bounded_pair_recorded' if len(result['rows']) == len(order) else 'bounded_pair_incomplete'
    result['all_cells_passed'] = len(result['rows']) == len(order) and all(r['status']=='pretrial_checked' for r in result['rows'])
    result['elapsed_seconds'] = time.monotonic()-started
    dump(args.output, result)


if __name__ == '__main__':
    main()
