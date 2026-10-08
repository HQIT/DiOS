"""Independent result/disk/SQLite and business-output checks, no producer import."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('result', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    result = json.loads(args.result.read_text(encoding='utf-8'))
    assert result['status'] == 'passed_execution_pending_independent_quality_check'
    assert len(result['rows']) == 27
    for path, checksum in result['source_sha256'].items():
        assert sha((ROOT / path).read_bytes()) == checksum
    for name, checksum in result['input_files_sha256'].items():
        assert sha((HERE / 'results' / name).read_bytes()) == checksum
    snapshots = json.loads((HERE / 'results/archive-snapshots-20260930-v1.json').read_text(encoding='utf-8'))
    snapshot = next(s for s in snapshots['snapshots'] if s['kind'] == 'issue')
    examples = json.loads((HERE / 'results/archive-development-examples-20260930-v3.json').read_text(encoding='utf-8'))
    example = next(e for e in examples['examples'] if e['source_record']['event_id'] == snapshot['source_event_id'])
    source = example['source_record']
    with gzip.open(ROOT / 'tmp/rr022-gharchive-scout-v1' / (source['archive_hour'] + '.json.gz'), 'rb') as stream:
        archived = next(line for i, line in enumerate(stream, 1) if i == source['line'])
    assert sha(archived) == source['raw_line_sha256']
    raw = Path(snapshot['cache_path']).read_bytes()
    assert canonical(json.loads(archived)['payload']['issue']) == raw
    assert sha(raw) == snapshot['content_sha256']
    issue = json.loads(raw)
    labels = []
    for item in issue['labels']:
        if item['name'] not in labels:
            labels.append(item['name'])
    wanted = dict(title_present=issue['title'].strip() != '',
                  title_characters=sum(1 for _ in issue['title']),
                  body_characters=sum(1 for _ in (issue['body'] or '')),
                  labels=sorted(labels), queue='needs_information')
    if wanted['title_present'] and (issue['body'] or '').strip() != '':
        wanted['queue'] = 'awaiting_triage'
    cases = {
        'normal': (1, 1, 1, True), 'other_read_object': (1, 0, 0, None),
        'wrong_read_version': (1, 0, 0, None), 'other_write_object': (1, 1, 0, None),
        'oversize_report': (1, 1, 0, None), 'wrong_report_type': (1, 1, 0, None),
        'wrong_content_control': (1, 1, 1, False),
        'unicode_content_control': (1, 1, 1, False), 'undeclared_event': (0, 0, 0, None),
    }
    groups, totals, quality, task_ids = defaultdict(list), defaultdict(Counter), [], set()
    for row in result['rows']:
        case, method, job, obs = row['case'], row['method'], row['job'], row['observed']
        tasks, reads, writes, correct = cases[case]
        assert row['pass'] and row['exit_code'] == 0 and obs['error'] is None
        assert obs['model_calls'] == 0 and obs['initial_task_count'] == 0
        assert row['expected'] == dict(tasks=tasks, reads=reads, writes=writes, report_correct=correct)
        assert obs['input_sha256'] == sha(json.dumps(job, sort_keys=True, ensure_ascii=False).encode())
        policy = job['constraints']
        assert policy['version'] == 2 and set(policy['tools']) == {'snapshot.read', 'report.write'}
        fields = policy['tools']['report.write']
        assert set(fields) == {'resource', 'version', 'source_event_id', 'report'}
        assert fields['report'] == {'type': 'string', 'minLength': 1, 'maxLength': 2048}
        assert set(policy['tools']['snapshot.read']) == {'resource', 'version', 'source_event_id'}
        assert obs['created_task_count'] == len(obs['tasks']) == tasks
        assert obs['policy']['decision'] == ('allow' if tasks else 'deny')
        for task in obs['tasks']:
            assert task['id'] not in task_ids
            task_ids.add(task['id'])
            assert task['context_id'] == obs['event_id'] and task['trace_id'] == obs['event_trace']
            assert task['status'] == 'submitted'
        for pk, hk, vk in [('ledger', 'ledger_sha256', 'operations'), ('reports_path', 'reports_sha256', 'reports')]:
            path = Path(obs[pk]).resolve()
            assert path.is_relative_to((ROOT / 'tmp/rr022-baselines').resolve())
            assert sha(path.read_bytes()) == obs[hk]
            assert [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()] == obs[vk]
        assert Counter(op['tool'] for op in obs['operations']) == Counter({'snapshot.read': reads, 'report.write': writes})
        assert len(obs['reports']) == writes
        if reads:
            op = obs['operations'][0]
            assert op['arguments'] == job['read_arguments']
            assert (op['content_sha256'], op['byte_count']) == (sha(raw), len(raw))
        actual_correct = None
        if writes:
            report = obs['reports'][0]
            assert report == obs['operations'][1]['arguments']
            assert report['resource'] == job['write_resource']
            assert report['version'] == snapshot['version'] and report['source_event_id'] == snapshot['source_event_id']
            try:
                actual_correct = json.loads(report['report']) == wanted
            except (ValueError, TypeError):
                actual_correct = False
            assert actual_correct is correct
            if 'report_override' in job:
                assert report['report'] == job['report_override']
        chain, previous = obs['audit_chain'], ''
        for i, entry in enumerate(chain):
            value = dict(entry); checksum = value.pop('entry_hash')
            assert value['sequence'] == i and value['trace_id'] == obs['event_trace'] and value['previous_hash'] == previous
            assert sha(canonical(value)) == checksum
            previous = checksum
        assert [e['stage'] for e in chain[:2]] == ['contract', 'policy']
        assert sum(e['stage'] == 'a2a_task' for e in chain) == tasks
        statuses = [] if not tasks else [403] if not reads else [200, 200 if writes else 403]
        if method == 'agentspec_host':
            assert not obs['grants'] and not [e for e in chain if e['stage'] == 'tool_call']
            if tasks:
                evaluations = obs['agentspec']['predicate_evaluations']
                assert len(evaluations) == len(statuses)
                assert [e['violation'] for e in evaluations] == [s == 403 for s in statuses]
        elif tasks:
            assert len(obs['grants']) == 1
            grant = obs['grants'][0]
            assert grant['task_id'] == obs['tasks'][0]['id'] and grant['event_log_id'] == obs['event_id'] and grant['trace_id'] == obs['event_trace']
            assert [c['status'] for c in obs['calls']] == statuses
            assert grant['call_count'] == len(statuses)
            assert [e['outcome'] for e in chain if e['stage'] == 'tool_call'] == ['allow' if s == 200 else 'deny' for s in statuses]
        else:
            assert not obs['grants']
        if method == 'opa':
            assert len(obs['admission_opa']) == 2 and len(obs['call_opa']) == len(statuses) * 2
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
        groups[case].append(row)
        totals[method].update(tasks=tasks, reads=reads, writes=writes, correct_reports=int(actual_correct is True), incorrect_reports=int(actual_correct is False))
        quality.append(dict(case=case, method=method, report_correct=actual_correct))
    assert set(groups) == set(cases)
    for rows in groups.values():
        assert len(rows) == 3 and {r['method'] for r in rows} == {'e2ag', 'opa', 'agentspec_host'}
        assert len({sha(canonical(r['job'])) for r in rows}) == 1
    output = dict(status='passed', result_sha256=sha(args.result.read_bytes()), checker_sha256=sha(Path(__file__).read_bytes()),
                  rows=27, databases_verified=27, task_links_verified=len(task_ids), independent_development_objects=1,
                  quality=quality, totals={m: dict(t) for m, t in totals.items()},
                  boundary='Development evidence, not holdout. Incorrect allowed content is a deliberate quality negative control, not an authorization failure.')
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in output.items() if k != 'quality'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
