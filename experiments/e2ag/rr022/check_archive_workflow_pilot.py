"""Independent disk/source/database checks; no imported governance evaluator."""
import argparse
import base64
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('result', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    result = json.loads(args.result.read_text(encoding='utf-8'))
    assert result['status'] == 'passed' and len(result['rows']) == 36
    sp = BASE / 'results/archive-snapshots-20260930-v1.json'
    ep = BASE / 'results/archive-development-examples-20260930-v3.json'
    assert sha(sp.read_bytes()) == result['snapshots_sha256']
    assert sha(ep.read_bytes()) == result['examples_sha256']
    acquisition = json.loads(sp.read_text(encoding='utf-8'))
    examples = json.loads(ep.read_text(encoding='utf-8'))['examples']
    snapshots = acquisition['snapshots']
    assert len(snapshots) == 3
    for request in acquisition['requests']:
        assert request['status'] == 200
        assert sha(Path(request['cache_path']).read_bytes()) == request['response_sha256']
    content = {}
    for s, example in zip(snapshots, examples):
        raw = Path(s['cache_path']).read_bytes()
        assert sha(raw) == s['content_sha256'] and len(raw) == s['bytes'] and len(raw.splitlines()) == s['line_count']
        assert s['source_event_id'] == example['source_record']['event_id']
        if s['kind'] in ('push', 'pr'):
            query = next(r for r in acquisition['requests'] if r['url'] == s['retrieval_url'])
            stored = json.loads(Path(query['cache_path']).read_text(encoding='utf-8'))
            assert base64.b64decode(stored['content']) == raw
            assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == stored['sha'] == s['git_blob_sha']
            assert s['retrieval_url'].endswith('?ref=' + example['adaptation']['target']['head'])
            commit = next(r for r in acquisition['requests'] if r['url'] == s['commit_api_url'])
            commit_json = json.loads(Path(commit['cache_path']).read_text(encoding='utf-8'))
            assert commit_json['sha'] == s['version'] and commit_json['tree']['sha'] == s['tree_sha']
        else:
            source = example['source_record']
            with gzip.open(ROOT / 'tmp/rr022-gharchive-scout-v1' / (source['archive_hour'] + '.json.gz'), 'rb') as f:
                archived = next(line for i, line in enumerate(f, 1) if i == source['line'])
            assert sha(archived) == source['raw_line_sha256']
            assert canon(json.loads(archived)['payload']['issue']) == raw
        content[s['kind']] = raw
    groups = defaultdict(list)
    summary = defaultdict(Counter)
    task_ids = set()
    for row in result['rows']:
        assert row['pass'] and row['exit_code'] == 0
        kind, scenario = row['case'].split(':')
        job, obs, method = row['job'], row['observed'], row['method']
        assert obs['error'] is None and obs['model_calls'] == 0 and obs['initial_task_count'] == 0
        assert obs['input_sha256'] == sha(json.dumps(job, sort_keys=True, ensure_ascii=False).encode())
        tasks = 0 if scenario == 'undeclared_event' else 1
        reads = 0 if scenario in ('undeclared_event', 'other_snapshot') else 1
        writes = 1 if scenario == 'normal' else 0
        assert row['expected'] == {'tasks': tasks, 'reads': reads, 'writes': writes}
        assert obs['created_task_count'] == len(obs['tasks']) == tasks
        assert obs['policy']['decision'] == ('allow' if tasks else 'deny')
        for task in obs['tasks']:
            assert task['id'] not in task_ids
            task_ids.add(task['id'])
            assert task['context_id'] == obs['event_id'] and task['trace_id'] == obs['event_trace']
            assert task['status'] == 'submitted'  # Native completion was not invoked.
        for path_key, hash_key, value_key in [('ledger', 'ledger_sha256', 'operations'), ('reports_path', 'reports_sha256', 'reports')]:
            path = Path(obs[path_key]).resolve()
            assert path.is_relative_to((ROOT / 'tmp/rr022-baselines').resolve())
            assert sha(path.read_bytes()) == obs[hash_key]
            assert [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] == obs[value_key]
        counts = Counter(op['tool'] for op in obs['operations'])
        assert counts == Counter({'snapshot.read': reads, 'report.write': writes})
        if reads:
            actual = obs['operations'][0]
            assert actual['arguments'] == job['read_arguments']
            raw = content[kind]
            assert (actual['content_sha256'], actual['byte_count'], actual['line_count']) == (sha(raw), len(raw), len(raw.splitlines()))
        if writes:
            report = obs['reports'][0]
            assert report == obs['operations'][1]['arguments'] == job['policy_arguments']['report.write']
            assert report['content_sha256'] == sha(content[kind]) and report['byte_count'] == len(content[kind])
        else:
            assert not obs['reports']
        chain = obs['audit_chain']
        previous = ''
        for i, entry in enumerate(chain):
            value = dict(entry); checksum = value.pop('entry_hash')
            assert value['sequence'] == i and value['trace_id'] == obs['event_trace'] and value['previous_hash'] == previous
            assert sha(canon(value)) == checksum
            previous = checksum
        assert [e['stage'] for e in chain[:2]] == ['contract', 'policy']
        assert len([e for e in chain if e['stage'] == 'a2a_task']) == tasks
        if method == 'agentspec_host':
            assert not obs['grants'] and not [e for e in chain if e['stage'] == 'tool_call']
            if tasks:
                predicates = obs['agentspec']['predicate_evaluations']
                assert len(predicates) == (1 if scenario == 'other_snapshot' else 2)
                assert predicates[-1]['violation'] == (scenario != 'normal')
                assert obs['agentspec']['candidates'][0]['arguments'] == job['read_arguments']
        elif tasks:
            assert len(obs['grants']) == 1
            grant = obs['grants'][0]
            assert grant['task_id'] == obs['tasks'][0]['id'] and grant['event_log_id'] == obs['event_id'] and grant['trace_id'] == obs['event_trace']
            statuses = [c['status'] for c in obs['calls']]
            assert statuses == {'normal': [200, 200], 'other_snapshot': [403], 'other_report': [200, 403]}[scenario]
            assert grant['call_count'] == len(statuses)
            call_entries = [e for e in chain if e['stage'] == 'tool_call']
            assert [e['outcome'] for e in call_entries] == ['allow' if s == 200 else 'deny' for s in statuses]
        else:
            assert not obs['grants']
        if method == 'opa':
            assert len(obs['admission_opa']) == 2
            assert len(obs['call_opa']) == (0 if not tasks else 2 if scenario == 'other_snapshot' else 4)
            assert all(e['error'] is None for e in obs['call_opa'])
        database = Path(obs['database']).resolve()
        assert database.is_relative_to((ROOT / 'tmp/rr022-baselines').resolve())
        db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            stored = db.execute('SELECT id, cloud_event, trace_id, audit_chain FROM event_logs').fetchall()
            assert len(stored) == 1 and stored[0][0] == obs['event_id'] and stored[0][2] == obs['event_trace']
            assert json.loads(stored[0][1]) == job['event'] and json.loads(stored[0][3]) == chain
            assert db.execute('SELECT COUNT(*) FROM a2a_tasks').fetchone()[0] == tasks
            assert db.execute('SELECT COUNT(*) FROM e2ag_tool_grants').fetchone()[0] == len(obs['grants'])
        finally:
            db.close()
        groups[row['case']].append(row)
        summary[method][scenario] += 1
    assert len(groups) == 12
    for rows in groups.values():
        assert {r['method'] for r in rows} == {'e2ag', 'opa', 'agentspec_host'} and len(rows) == 3
        assert len({sha(canon(r['job'])) for r in rows}) == 1
    for path, checksum in result['source_sha256'].items():
        assert sha((ROOT / path).read_bytes()) == checksum
    output = {'status': 'passed', 'result_sha256': sha(args.result.read_bytes()), 'checker_sha256': sha(Path(__file__).read_bytes()),
              'source_snapshots': 3, 'unique_content_sha256': len({sha(v) for v in content.values()}),
              'rows': 36, 'databases_verified': 36, 'task_links_verified': len(task_ids),
              'summary': {m: dict(s) for m, s in summary.items()},
              'boundary': 'Deterministic workflow completion, native task still submitted. AgentSpec uses shared DiOS admission; not independently reproduced full governance.'}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
