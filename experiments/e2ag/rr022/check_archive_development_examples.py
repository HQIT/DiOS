"""Cross-check example objects and labels directly against archived source bytes.

Does not import adapter, task builder, oracle or E2AG authorization code.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('examples', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing overwrite')
    result = json.loads(args.examples.read_text(encoding='utf-8'))
    assert result['status'] == 'development_examples_built_not_governance_results'
    assert len(result['examples']) == 3
    checked = []
    for item in result['examples']:
        source = item['source_record']
        archive = ROOT / 'tmp/rr022-gharchive-scout-v1' / (source['archive_hour'] + '.json.gz')
        with gzip.open(archive, 'rb') as stream:
            raw = next(line for number, line in enumerate(stream, 1) if number == source['line'])
        assert hashlib.sha256(raw).hexdigest() == source['raw_line_sha256']
        e = json.loads(raw)
        assert e['id'] == source['event_id']
        assert e['public'] is True
        repo, p = e['repo']['name'], e['payload']
        if e['type'] == 'PushEvent':
            target = {'kind': 'push', 'repository': repo, 'push_id': p['push_id'], 'ref': p['ref'],
                      'before': p['before'], 'head': p['head']}
            identity, version = p['push_id'], p['head']
            expected_type, subject = 'git.push', p['ref']
        elif e['type'] == 'PullRequestEvent':
            pr = p['pull_request']
            target = {'kind': 'pr', 'repository': repo, 'number': p['number'],
                      'head': pr['head']['sha'], 'base': pr['base']['sha']}
            identity, version = p['number'], pr['head']['sha']
            expected_type, subject = 'git.pull_request.created', f'pr/{identity}'
        else:
            assert e['type'] == 'IssuesEvent'
            version = hashlib.sha256(json.dumps(p['issue'], sort_keys=True, ensure_ascii=False,
                                                separators=(',', ':')).encode()).hexdigest()
            identity = p['issue']['number']
            target = {'kind': 'issue', 'repository': repo, 'number': identity, 'snapshot_sha256': version}
            expected_type, subject = 'git.issue.opened', f'issue/{identity}'
        assert target == item['adaptation']['target']
        normalized = item['normalized_event']
        assert normalized['type'] == expected_type and normalized['subject'] == subject
        assert normalized['source'] == f'github/{repo}'
        assert normalized['id'] == 'gharchive:' + e['id'] and normalized['time'] == e['created_at']
        assert normalized['data']['_archive_provenance']['original_webhook_signature_verified'] is False
        resource = f'rr022-dev/{repo}/{target["kind"]}/{identity}'
        task = item['task']
        args_expected = {'resource': resource, 'version': version, 'source_event_id': e['id'], 'record_count': 1}
        assert task['call'] == {'tool': 'event.record', 'arguments': args_expected}
        assert task['policy'] == dict(tool='event.record', **args_expected)
        assert task['expected_artifact']['target'] == target
        cases = item['expected_cases']
        assert [c['name'] for c in cases] == ['normal', 'different_object', 'different_version_or_snapshot']
        assert [c['expected']['allow'] for c in cases] == [True, False, False]
        assert [c['expected_receipts'] for c in cases] == [1, 0, 0]
        assert cases[0]['call'] == task['call']
        other = item['different_object_provenance']
        assert other['repository'] == repo and other['event_type'] == source['event_type']
        assert other['object_group'] != source['object_group']
        other_archive = ROOT / 'tmp/rr022-gharchive-scout-v1' / (other['archive_hour'] + '.json.gz')
        with gzip.open(other_archive, 'rb') as stream:
            other_raw = next(line for number, line in enumerate(stream, 1) if number == other['line'])
        assert hashlib.sha256(other_raw).hexdigest() == other['raw_line_sha256']
        other_event = json.loads(other_raw)
        assert other_event['id'] == other['event_id']
        op = other_event['payload']
        other_id = op['push_id'] if target['kind'] == 'push' else op['number'] if target['kind'] == 'pr' else op['issue']['number']
        assert cases[1]['call']['arguments']['resource'] == f'rr022-dev/{repo}/{target["kind"]}/{other_id}'
        for case, changed in zip(cases[1:], ['resource', 'version']):
            assert case['call']['tool'] == 'event.record'
            differences = {key for key in args_expected if case['call']['arguments'][key] != args_expected[key]}
            assert differences == {changed}
            assert case['expected']['reason'] == changed + '_mismatch'
        checked.append({'event_id': e['id'], 'source_to_task_checked': True, 'labels_checked': 3})
    tests = subprocess.run([sys.executable, str(BASE / 'test_archive_development_adapter.py')],
                           capture_output=True, text=True, timeout=30)
    assert tests.returncode == 0, tests.stderr
    output = {'status': 'passed', 'examples_sha256': digest(args.examples),
              'checker_sha256': digest(Path(__file__)), 'adapter_sha256': digest(BASE / 'archive_development_adapter.py'),
              'source_checks': checked, 'tests_stdout': tests.stdout, 'tests_stderr': tests.stderr,
              'boundary': 'Checks example construction and labels only; zero governance/PEP runs and zero tool executions.'}
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
