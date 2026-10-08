"""Read-only verification/attribution of saved pretrial records, not a rerun."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    source = json.loads(args.input.read_text(encoding='utf-8'))
    assert source['status'] != 'running'
    result = dict(status='checking', source_sha256=sha(args.input), completed_cells=len(source['rows']), planned_cells=len(source['planned_order']), rows=[])
    for cell in source['rows']:
        if 'raw_sha256' not in cell:
            result['rows'].append(dict(kind=cell['kind'], mode=cell['mode'], concurrency=cell['concurrency'], not_measured=True, error=cell.get('error')))
            continue
        directory = Path(cell['directory'])
        assert sha(directory/'client-raw.json') == cell['raw_sha256']
        for name, expected in cell['verification']['evidence_sha256'].items(): assert sha(directory/name)==expected
        raw = json.loads((directory/'client-raw.json').read_text(encoding='utf-8'))
        spans = json.loads((directory/'server-spans.json').read_text(encoding='utf-8'))
        operations = [json.loads(s) for s in (directory/'operations.jsonl').read_text(encoding='utf-8').splitlines()]
        reports = [json.loads(s) for s in (directory/'reports.jsonl').read_text(encoding='utf-8').splitlines()]
        rows = [r for r in raw['records'] if r['phase']=='measurement']
        good = [r for r in rows if r['success']]
        ids = [r['request_id'] for r in raw['records']]
        assert len(ids) == len(set(ids))
        assert len(rows)==cell['summary']['measured_attempts'] and len(good)==cell['summary']['completed']
        for r in raw['records']:
            assert r['end_ns'] >= r['planned_ns']
            for c in r['calls']: assert r['planned_ns'] <= c['start_ns'] <= c['end_ns'] <= r['end_ns']
        latencies = sorted((r['end_ns']-r['planned_ns'])/1e6 for r in good)
        for q, p in [('p50_ms',.5),('p95_ms',.95)]:
            assert cell['summary']['workflow'][q] == (latencies[math.ceil(len(latencies)*p)-1] if latencies else None)
        windows = [w for w in raw['windows'] if w['phase']=='measurement']
        if windows:
            w=windows[0]; end=w['start_ns']+w['configured_seconds']*1e9
            inside=sum(r['end_ns']<=end for r in good)
            assert cell['summary']['completed_inside_window']==inside
            assert all(r['planned_ns']>=w['start_ns'] for r in rows)
        report_ids=Counter(r['rpc_id'] for r in reports)
        op_ids=Counter(r['rpc_id'] for r in operations)
        for r in raw['records']:
            if r['success']:
                assert op_ids[r['request_id']+':read']==op_ids[r['request_id']+':write']==report_ids[r['request_id']+':write']==1
        with sqlite3.connect(directory/'state.db') as db:
            for r in raw['records']:
                if not r['success']: continue
                tasks=list(db.execute('SELECT context_id, trace_id, status FROM a2a_tasks WHERE context_id=?',(r['event_id'],)))
                assert tasks==[(r['event_id'], r['trace_id'], 'submitted')]
        stage_samples=defaultdict(list)
        commit_counts=Counter()
        for span in spans:
            assert span['end_ns']>=span['start_ns']
            if not span['request_id'].startswith('measurement-'): continue
            stage_samples[span['stage']].append((span['end_ns']-span['start_ns'])/1e6)
            if span['stage']=='db_commit': commit_counts[span['request_id'].rsplit(':',1)[-1]] += 1
        resources=raw['resources']
        processes=[]
        identities = cell.get('process_identity')
        # v1 observed Windows venv launchers, not their Python worker children.
        # Preserve raw values but do not elevate these to service resource data.
        if identities:
            assert [p['pid'] for p in resources[0]['processes'][1:]] == [p['service_pid'] for p in identities]
            for index, role in enumerate(['driver','upstream','app']):
                records=[r['processes'][index] for r in resources]
                processes.append(dict(role=role, max_rss_bytes=max(r['rss_bytes'] for r in records),
                    max_cpu_percent_one_core=max(r['cpu_percent_one_core'] for r in records)))
        result['rows'].append(dict(kind=cell['kind'], mode=cell['mode'], concurrency=cell['concurrency'], status=cell['status'],
            proxy_transport=cell.get('proxy_transport', 'per-call'),
            measured_attempts=len(rows), successful=len(good), errors=len(rows)-len(good),
            stages={stage:dict(n=len(values), median_ms=statistics.median(values), max_ms=max(values)) for stage,values in stage_samples.items()},
            commit_counts_by_request_phase=dict(commit_counts), processes=processes, service_resource_identity_verified=bool(identities),
            service_resource_limitation=None if identities else 'v1 sampled launcher PIDs; service CPU/RSS invalid; global memory and disk samples unaffected',
            min_memory_available_fraction=min(r['memory_available']/r['memory_total'] for r in resources),
            min_disk_free=min(r['disk_free'] for r in resources),
            report_content_digests=sorted(set(hashlib.sha256(r['arguments']['report'].encode()).hexdigest() for r in reports))))
    result['status']='saved_records_verified'
    result['measured_attempts']=sum(r.get('measured_attempts',0) for r in result['rows'])
    result['successful']=sum(r.get('successful',0) for r in result['rows'])
    result['errors']=sum(r.get('errors',0) for r in result['rows'])
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:result[k] for k in ['status','completed_cells','planned_cells','measured_attempts','successful','errors']}))


if __name__=='__main__': main()
