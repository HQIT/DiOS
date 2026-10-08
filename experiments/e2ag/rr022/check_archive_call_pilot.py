"""Independent result audit: pairing, immutable examples, disk receipts, hashes.

Does not import driver, method implementations or oracle.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_hash(obj, compact=False):
    options = {'sort_keys': True, 'ensure_ascii': False}
    if compact:
        options['separators'] = (',', ':')
    return hashlib.sha256(json.dumps(obj, **options).encode()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('result', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing overwrite')
    result = json.loads(args.result.read_text(encoding='utf-8'))
    assert result['status'] == 'passed'
    example_file = BASE / 'results/archive-development-examples-20260930-v3.json'
    assert file_hash(example_file) == result['examples_sha256']
    examples = json.loads(example_file.read_text(encoding='utf-8'))['examples']
    expectations = {e['adaptation']['target']['kind'] + ':' + c['name']: (e, c)
                    for e in examples for c in e['expected_cases']}
    groups = defaultdict(list)
    summary = defaultdict(Counter)
    ledger_paths = set()
    for row in result['rows'] + result['sink_controls']:
        method, job, observed = row['method'], row['job'], row['observed']
        assert row['pass'] and row['exit_code'] == 0 and observed['error'] is None
        assert set(job) == {'event', 'policy', 'call', 'task', 'host'}
        assert observed['input_sha256'] == object_hash(job)
        assert observed['model_calls'] == 0
        policy, call = job['policy'], job['call']
        expected_args = {k: policy[k] for k in ('resource', 'version', 'source_event_id', 'record_count')}
        # Independently derive the prescribed decision from raw policy and actual arguments.
        permitted = (call['tool'] == policy['tool'] and call['arguments'] == expected_args
                     and type(call['arguments']['record_count']) is int)
        count = 1 if method == 'unguarded' or permitted else 0
        assert row['expected_records'] == count
        assert observed['decision'] == ('allow' if count else 'deny')
        ledger = Path(observed['ledger']).resolve()
        assert ledger.is_relative_to((ROOT / 'tmp/rr022-baselines').resolve())
        assert str(ledger) not in ledger_paths
        ledger_paths.add(str(ledger))
        assert file_hash(ledger) == observed['ledger_sha256']
        records = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
        assert records == observed['records'] and len(records) == count
        assert len(observed['arrivals']) == count
        for sequence, (record, request) in enumerate(zip(records, observed['arrivals']), 1):
            assert record == {'sequence': sequence, 'tool': call['tool'], 'arguments': call['arguments'], 'source_snapshot': job['event']}
            assert request == {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': call['tool'], 'arguments': call['arguments']}}
        if method in ('e2ag', 'opa'):
            assert observed['response']['status'] == (200 if permitted else 403)
            assert observed['grant_status'] == 'active' and observed['grant_call_count'] == 1
            chain = observed['audit_chain']
            assert len(chain) == 2 and [e['stage'] for e in chain] == ['tool_grant', 'tool_call']
            previous_hash = ''
            for i, entry in enumerate(chain):
                value = dict(entry)
                checksum = value.pop('entry_hash')
                assert value['sequence'] == i and value['trace_id'] == job['task']['trace_id']
                assert value['previous_hash'] == previous_hash
                assert object_hash(value, compact=True) == checksum
                previous_hash = checksum
            assert observed['call_audit'] == [chain[-1]]
            assert chain[-1]['outcome'] == ('allow' if permitted else 'deny')
            assert chain[-1]['evidence']['argument_constraints_sha256'] == object_hash(observed['constraints'], compact=True)
            database = Path(observed['database']).resolve()
            assert database.is_relative_to((ROOT / 'tmp/rr022-baselines').resolve())
            db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                db.execute('PRAGMA query_only=ON')
                stored = db.execute('SELECT cloud_event, audit_chain FROM event_logs').fetchall()
                assert len(stored) == 1
                assert json.loads(stored[0][0]) == job['event']
                assert json.loads(stored[0][1]) == chain
            finally:
                db.close()
        if method == 'opa':
            evaluations = observed['opa_evaluations']
            assert len(evaluations) == 2
            assert [e['input']['phase'] for e in evaluations] == ['grant', 'call']
            assert all(e['error'] is None for e in evaluations)
            assert evaluations[0]['result']['allow'] is True
            assert evaluations[1]['result']['allow'] == permitted
            assert evaluations[1]['input']['call']['arguments'] == call['arguments']
        if method == 'agentspec':
            assert observed['predicate_evaluations'] == [{'arguments': call['arguments'], 'violation': not permitted}]
            assert observed['upstream_commit'] == result['agentspec_commit']
            assert observed['audit_chain'] is None  # Driver evidence is not a native audit chain.
        if method == 'unguarded':
            assert row['case'].endswith(':different_object') and not permitted
        else:
            groups[row['case']].append(row)
            label = 'control' if row['control'] else 'main'
            summary[method][label + '_rows'] += 1
            summary[method][label + '_allow' if permitted else label + '_deny'] += 1
        if not row['control']:
            e, c = expectations[row['case']]
            assert job['event'] == e['normalized_event'] and job['policy'] == e['task']['policy']
            assert job['call'] == c['call'] and row['expected_records'] == c['expected_receipts']
    assert len(result['sink_controls']) == 3 and len(result['rows']) == 48
    assert len(groups) == 16
    for name, rows in groups.items():
        assert {r['method'] for r in rows} == {'e2ag', 'agentspec', 'opa'}
        assert len(rows) == 3
        assert len({object_hash(r['job']) for r in rows}) == 1
    assert all(s['main_rows'] == 9 and s['main_allow'] == 3 and s['main_deny'] == 6 and s['control_rows'] == 7
               for s in summary.values())
    for path, checksum in result['source_sha256'].items():
        assert file_hash(ROOT / path) == checksum, path
    output = {'status': 'passed', 'result_sha256': file_hash(args.result), 'checker_sha256': file_hash(Path(__file__)),
              'method_summary': {key: dict(value) for key, value in summary.items()},
              'paired_main_cases': 9, 'paired_diagnostic_controls': 7, 'unguarded_sink_controls': 3,
              'distinct_ledgers_verified': len(ledger_paths), 'native_databases_verified': 32,
              'boundary': 'Development call-boundary only; no admission or full Agent benchmark. AgentSpec driver log not scored as native audit.'}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
