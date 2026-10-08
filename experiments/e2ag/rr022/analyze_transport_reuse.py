"""Read-only transport observations; never pool old and new timing samples."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    source = json.loads(args.input.read_text(encoding='utf-8'))
    assert source['status'] != 'running'
    rows = []
    for cell in source['rows']:
        directory = Path(cell['directory'])
        assert sha(directory/'client-raw.json') == cell['raw_sha256']
        for name, digest in cell['verification']['evidence_sha256'].items():
            assert sha(directory/name) == digest
        raw = json.loads((directory/'client-raw.json').read_text(encoding='utf-8'))
        operations = [json.loads(line) for line in (directory/'operations.jsonl').read_text(encoding='utf-8').splitlines()]
        spans = json.loads((directory/'server-spans.json').read_text(encoding='utf-8'))
        samples = [(s['end_ns']-s['start_ns'])/1e6 for s in spans if s['stage']=='proxy_client_init' and s['request_id'].startswith('measurement-')]
        startup = [(s['end_ns']-s['start_ns'])/1e6 for s in spans if s['stage']=='proxy_transport_startup']
        rpc_counts = Counter(o['rpc_id'] for o in operations)
        assert max(rpc_counts.values(), default=0) <= 1, 'Repeated upstream operation'
        with sqlite3.connect(directory/'state.db') as db:
            invocations = list(db.execute('SELECT id,state,event_log_id FROM e2ag_tool_invocations'))
            chains = {key:json.loads(chain) for key,chain in db.execute('SELECT id,audit_chain FROM event_logs')}
            for invocation_id, state, event in invocations:
                assert sum(e['stage']=='tool_intent' and e['evidence'].get('invocation_id')==invocation_id for e in chains[event]) == 1
                if state == 'response_received':
                    assert sum(e['stage']=='tool_call' and e['evidence'].get('invocation_id')==invocation_id for e in chains[event]) == 1
            # Successful client calls carry a server-generated id; match each to
            # both the event chain and persisted invocation, independent of totals.
            inventory = {(i[0],i[2]):i[1] for i in invocations}
            if cell['mode'] == 'G2':
                for record in raw['records']:
                    if not record['success']:
                        continue
                    for call in record['calls']:
                        assert inventory[(call['invocation_id'], record['event_id'])] == 'response_received'
        rows.append(dict(kind=cell['kind'], mode=cell['mode'], proxy_transport=cell.get('proxy_transport','per-call'),
                         concurrency=cell['concurrency'], status=cell['status'], stop_reason=cell.get('stop_reason'),
                         measurement_attempts=cell['summary']['measured_attempts'], measurement_errors=cell['summary']['errors'],
                         warmup_attempts=sum(r['phase']=='warmup' for r in raw['records']),
                         warmup_errors=sum(r['phase']=='warmup' and not r['success'] for r in raw['records']),
                         successful_workflow_p50_ms=cell['summary']['workflow']['p50_ms'],
                         proxy_client_init_measurement_n=len(samples), proxy_client_init_median_ms=statistics.median(samples) if samples else None,
                         proxy_transport_startup_ms=startup,
                         all_phase_upstream_operations=len(operations), all_phase_distinct_tcp_peer_ports=len({o['client_port'] for o in operations}),
                         invocation_states=dict(Counter(i[1] for i in invocations)),
                         min_memory_available_fraction=min(r['memory_available']/r['memory_total'] for r in raw['resources'])))
    result = dict(status='transport_and_invocation_records_verified', input_sha256=sha(args.input), rows=rows,
                  boundary='Descriptive short-run observations; distinct TCP peer ports are not a general connection-count metric. No confidence interval, capacity, exactly-once or unique-lock-cause claim.')
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(status=result['status'], rows=len(rows), measured_attempts=sum(r['measurement_attempts'] for r in rows),
                          measured_errors=sum(r['measurement_errors'] for r in rows), warmup_errors=sum(r['warmup_errors'] for r in rows))))


if __name__ == '__main__':
    main()
