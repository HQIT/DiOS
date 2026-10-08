"""Bounded socket fault tests on the existing development input; no holdout."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from performance_pretrial import ROOT, dump, run_cell


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--proxy-transport', choices=['per-call', 'shared'], default='per-call')
    parser.add_argument('--db-admission', choices=['parallel', 'queued'], default='parallel')
    args = parser.parse_args()
    assert not args.output.exists()
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.services.e2ag import verify_audit_chain
    sources = [Path(__file__), Path(__file__).with_name('performance_pretrial.py'), Path(__file__).with_name('storage_runtime.py'),
               *(ROOT / 'backend/app' / p for p in ('api/internal/e2ag_mcp.py', 'services/e2ag_tool_gateway.py', 'services/e2ag_http_transport.py', 'models/tables.py'))]
    result = dict(status='running', boundary='Three local processes, development issue only; injected commit errors, not performance or exactly-once evidence.',
                  source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}, proxy_transport=args.proxy_transport, db_admission=args.db_admission, rows=[])
    dump(args.output, result)
    for fault, reads, writes, unknown in [('intent_commit', 0, 0, 0), ('read_result_commit', 1, 0, 1), ('write_result_commit', 1, 1, 1)]:
        cell = run_cell('issue', 'G2', 1, 0, .01, persistence_fault=fault, proxy_transport=args.proxy_transport, db_admission=args.db_admission, request_limit=1)
        assert cell.get('verification', {}).get('verified'), cell
        directory = Path(cell['directory'])
        raw = json.loads((directory / 'client-raw.json').read_text(encoding='utf-8'))
        operations = [json.loads(line) for line in (directory / 'operations.jsonl').read_text(encoding='utf-8').splitlines()]
        reports = [json.loads(line) for line in (directory / 'reports.jsonl').read_text(encoding='utf-8').splitlines()]
        assert len(raw['records']) == 1 and not raw['records'][0]['success']
        assert raw['records'][0]['calls'][-1]['status'] == 503
        error = raw['records'][0]['calls'][-1]['error_data']
        if fault == 'intent_commit':
            assert error['reason'] == 'MCP_INTENT_PERSIST_FAILED' and error['forwarded'] is False
        else:
            assert error['reason'] == 'MCP_RESULT_PERSIST_FAILED'
            assert error['execution_outcome'] == 'unknown' and error['automatic_retry'] is False
        assert sum(o['tool'] == 'snapshot.read' for o in operations) == reads
        assert sum(o['tool'] == 'report.write' for o in operations) == len(reports) == writes
        with sqlite3.connect(directory / 'state.db') as db:
            invocations = list(db.execute('select id, state, request_sha256 from e2ag_tool_invocations'))
            chain = json.loads(db.execute('select audit_chain from event_logs').fetchone()[0])
            assert verify_audit_chain(chain)
            assert sum(i[1] == 'outcome_unknown' for i in invocations) == unknown
            if unknown:
                assert error['invocation_id'] in {i[0] for i in invocations if i[1] == 'outcome_unknown'}
            assert len(invocations) == reads + writes
            intents = [e for e in chain if e['stage'] == 'tool_intent']
            calls = [e for e in chain if e['stage'] == 'tool_call']
            assert {i[0] for i in invocations} == {e['evidence']['invocation_id'] for e in intents}
            assert len(calls) == (1 if fault == 'write_result_commit' else 0)
            # Intent digest links the actual JSON-RPC request to upstream ledger.
            config = json.loads((directory / 'config.json').read_text(encoding='utf-8'))
            # Snapshot read request is deterministic in the existing driver.
            for op in operations:
                if op['tool'] != 'snapshot.read':
                    continue
                payload = {'jsonrpc': '2.0', 'id': op['rpc_id'], 'method': 'tools/call',
                           'params': {'name': 'snapshot.read', 'arguments': config['job']['read_arguments']}}
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
                assert digest in {i[2] for i in invocations}
        row = dict(fault=fault, verified=True, upstream_reads=reads, upstream_writes=writes,
                   durable_unknown=unknown, response_received=len(invocations)-unknown, cell=cell)
        result['rows'].append(row)
        dump(args.output, result)
        print(json.dumps({k:v for k,v in row.items() if k != 'cell'}), flush=True)
    result['status'] = 'bounded_fault_checks_passed'
    dump(args.output, result)


if __name__ == '__main__':
    main()
