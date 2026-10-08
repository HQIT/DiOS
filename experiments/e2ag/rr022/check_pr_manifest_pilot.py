"""Independent source/Git tree, SQL-derived inventory, disk and native DB audit."""
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


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def verify_tree(tree):
    """Reconstruct every Git tree object, including the root, from flat entries."""
    assert tree['truncated'] is False
    entries = {e['path']: e for e in tree['tree']}
    assert len(entries) == len(tree['tree'])
    directories = {'': tree['sha'], **{p: e['sha'] for p, e in entries.items() if e['type'] == 'tree'}}
    children = defaultdict(list)
    for path, entry in entries.items():
        parent, _, name = path.rpartition('/')
        assert parent in directories and name
        children[parent].append((name, entry))
    for directory, expected in directories.items():
        ordered = sorted(children[directory], key=lambda pair: pair[0].encode() + (b'/' if pair[1]['type'] == 'tree' else b'\0'))
        raw = b''.join(e['mode'].lstrip('0').encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(e['sha']) for name, e in ordered)
        assert hashlib.sha1(b'tree ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == expected
    return len(directories)


def independent_inventory(bundle):
    db = sqlite3.connect(':memory:')
    try:
        for side in ('before', 'after'):
            db.execute(f'CREATE TABLE {side}_items (path TEXT PRIMARY KEY, mode TEXT, type TEXT, sha TEXT)')
            db.executemany(f'INSERT INTO {side}_items VALUES (?,?,?,?)', [(e['path'], e['mode'], e['type'], e['sha'])
                           for e in bundle[side]['tree'] if e['type'] != 'tree'])
        rows = db.execute('''
            SELECT a.path, b.mode, b.type, b.sha, a.mode, a.type, a.sha
            FROM after_items a LEFT JOIN before_items b ON a.path=b.path
            WHERE b.path IS NULL OR a.mode<>b.mode OR a.type<>b.type OR a.sha<>b.sha
            UNION ALL
            SELECT b.path, b.mode, b.type, b.sha, NULL,NULL,NULL
            FROM before_items b LEFT JOIN after_items a ON b.path=a.path WHERE a.path IS NULL
            ORDER BY 1
        ''').fetchall()
    finally:
        db.close()
    changes = []
    for path, bm, bt, bs, am, at, ash in rows:
        changes.append(dict(path=path, change='added' if bm is None else 'deleted' if am is None else 'modified',
                            before=None if bm is None else dict(mode=bm, type=bt, sha=bs),
                            after=None if am is None else dict(mode=am, type=at, sha=ash)))
    counts = {name: sum(c['change'] == name for c in changes) for name in ('added', 'deleted', 'modified')}
    counts['submodule_paths'] = sum(any(c[s] and c[s]['type'] == 'commit' for s in ('before', 'after')) for c in changes)
    return {**{k: bundle[k] for k in ('repository', 'number', 'base', 'head', 'merge_base')}, 'changes': changes, 'counts': counts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('result', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    result = json.loads(args.result.read_text(encoding='utf-8'))
    assert result['status'] == 'passed_execution_pending_independent_quality_check' and len(result['rows']) == 18
    for name, checksum in result['source_sha256'].items():
        assert sha((ROOT / name).read_bytes()) == checksum
    path = Path(result['acquisition_path'])
    assert sha(path.read_bytes()) == result['acquisition_sha256']
    acquisition = json.loads(path.read_text(encoding='utf-8'))
    assert acquisition['status'] == 'complete' and len(acquisition['requests']) == 5
    responses = []
    for request in acquisition['requests']:
        raw = Path(request['cache_path']).read_bytes()
        assert request['status'] == 200 and sha(raw) == request['response_sha256'] and len(raw) == request['bytes']
        responses.append(json.loads(raw))
    comparison, before_commit, after_commit, before_tree, after_tree = responses
    target = acquisition['target']
    assert comparison['base_commit']['sha'] == target['base']
    assert comparison['merge_base_commit']['sha'] == before_commit['sha'] == acquisition['merge_base']
    assert after_commit['sha'] == target['head']
    assert before_commit['tree']['sha'] == before_tree['sha'] and after_commit['tree']['sha'] == after_tree['sha']
    verified_trees = verify_tree(before_tree) + verify_tree(after_tree)
    raw = Path(acquisition['snapshot']['cache_path']).read_bytes()
    assert sha(raw) == acquisition['snapshot']['content_sha256'] == acquisition['snapshot']['version']
    bundle = json.loads(raw)
    assert bundle['base'] == target['base'] and bundle['head'] == target['head'] and bundle['merge_base'] == before_commit['sha']
    for side, tree in [('before', before_tree), ('after', after_tree)]:
        assert bundle[side] == dict(sha=tree['sha'], truncated=False, tree=[{k: e[k] for k in ('path', 'mode', 'type', 'sha')} for e in tree['tree']])
    examples = HERE / 'results/archive-development-examples-20260930-v3.json'
    assert sha(examples.read_bytes()) == result['examples_sha256'] == acquisition['examples_sha256']
    example = next(e for e in json.loads(examples.read_text(encoding='utf-8'))['examples'] if e['source_record']['event_id'] == acquisition['source_event_id'])
    source = example['source_record']
    with gzip.open(ROOT / 'tmp/rr022-gharchive-scout-v1' / (source['archive_hour'] + '.json.gz'), 'rb') as stream:
        line = next(v for i, v in enumerate(stream, 1) if i == source['line'])
    assert sha(line) == source['raw_line_sha256']
    pr = json.loads(line)['payload']['pull_request']
    assert pr['head']['sha'] == bundle['head'] and pr['base']['sha'] == bundle['base'] and pr['number'] == bundle['number']
    wanted = independent_inventory(bundle)
    expected = {'normal': (1, 1, 1, True), 'other_read_object': (1, 0, 0, None),
                'wrong_read_version': (1, 0, 0, None), 'other_write_object': (1, 1, 0, None),
                'wrong_content_control': (1, 1, 1, False), 'undeclared_event': (0, 0, 0, None)}
    groups, totals, quality, task_ids = defaultdict(list), defaultdict(Counter), [], set()
    for row in result['rows']:
        case, method, job, obs = row['case'], row['method'], row['job'], row['observed']
        tasks, reads, writes, correct = expected[case]
        assert row['passed'] and row['exit_code'] == 0 and obs['error'] is None and obs['model_calls'] == 0
        assert row['expected'] == dict(tasks=tasks, reads=reads, writes=writes, report_correct=correct)
        assert obs['input_sha256'] == sha(json.dumps(job, sort_keys=True, ensure_ascii=False).encode())
        fields = job['constraints']['tools']['report.write']
        assert fields['report'] == dict(type='string', minLength=1, maxLength=65536)
        assert set(fields) == {'resource', 'version', 'source_event_id', 'report'}
        assert obs['created_task_count'] == len(obs['tasks']) == tasks and obs['initial_task_count'] == 0
        assert obs['policy']['decision'] == ('allow' if tasks else 'deny')
        for task in obs['tasks']:
            assert task['id'] not in task_ids and task['status'] == 'submitted'
            task_ids.add(task['id'])
            assert task['context_id'] == obs['event_id'] and task['trace_id'] == obs['event_trace']
        for pk, hk, vk in [('ledger', 'ledger_sha256', 'operations'), ('reports_path', 'reports_sha256', 'reports')]:
            disk = Path(obs[pk]).resolve()
            assert disk.is_relative_to((ROOT / 'tmp/rr022-baselines').resolve())
            assert sha(disk.read_bytes()) == obs[hk]
            assert [json.loads(s) for s in disk.read_text(encoding='utf-8').splitlines()] == obs[vk]
        assert Counter(op['tool'] for op in obs['operations']) == Counter({'snapshot.read': reads, 'report.write': writes})
        assert len(obs['reports']) == writes
        if reads:
            op = obs['operations'][0]
            assert op['arguments'] == job['read_arguments'] and op['content_sha256'] == sha(raw) and op['byte_count'] == len(raw)
        actual_correct = None
        if writes:
            artifact = obs['reports'][0]
            assert artifact == obs['operations'][1]['arguments']
            assert artifact['resource'] == job['write_resource'] and artifact['version'] == sha(raw) and artifact['source_event_id'] == acquisition['source_event_id']
            actual_correct = json.loads(artifact['report']) == wanted
            assert actual_correct is correct
        chain, previous = obs['audit_chain'], ''
        for i, entry in enumerate(chain):
            value = dict(entry); checksum = value.pop('entry_hash')
            assert value['sequence'] == i and value['trace_id'] == obs['event_trace'] and value['previous_hash'] == previous
            assert sha(canon(value)) == checksum
            previous = checksum
        assert [e['stage'] for e in chain[:2]] == ['contract', 'policy']
        assert sum(e['stage'] == 'a2a_task' for e in chain) == tasks
        statuses = [] if not tasks else [403] if not reads else [200, 200 if writes else 403]
        if method == 'agentspec_host':
            assert not obs['grants'] and not [e for e in chain if e['stage'] == 'tool_call']
            if tasks:
                assert [e['violation'] for e in obs['agentspec']['predicate_evaluations']] == [s == 403 for s in statuses]
        elif tasks:
            assert len(obs['grants']) == 1 and [c['status'] for c in obs['calls']] == statuses
            grant = obs['grants'][0]
            assert grant['task_id'] == obs['tasks'][0]['id'] and grant['event_log_id'] == obs['event_id'] and grant['trace_id'] == obs['event_trace']
            assert grant['constraints'] == job['constraints'] and grant['call_count'] == len(statuses)
            assert [e['outcome'] for e in chain if e['stage'] == 'tool_call'] == ['allow' if s == 200 else 'deny' for s in statuses]
        else:
            assert not obs['grants']
        if method == 'opa':
            assert len(obs['admission_opa']) == 2 and len(obs['call_opa']) == 2 * len(statuses)
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
    assert set(groups) == set(expected)
    for rows in groups.values():
        assert len(rows) == 3 and {r['method'] for r in rows} == {'e2ag', 'opa', 'agentspec_host'}
        assert len({sha(canon(r['job'])) for r in rows}) == 1
    output = dict(status='passed', result_sha256=sha(args.result.read_bytes()), checker_sha256=sha(Path(__file__).read_bytes()),
                  rows=18, databases_verified=18, task_links_verified=len(task_ids), git_tree_objects_verified=verified_trees,
                  development_objects=1, report_counts=wanted['counts'], changed_paths=[c['path'] for c in wanted['changes']],
                  quality=quality, totals={m: dict(t) for m, t in totals.items()},
                  boundary='One previously used PR; not holdout, not autonomous model evaluation or native task completion.')
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in output.items() if k not in ('quality', 'changed_paths')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
