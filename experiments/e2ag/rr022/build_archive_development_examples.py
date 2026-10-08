"""Three earliest eligible VS Code examples from frozen exploration archives.

Builds examples + labels; runs the existing normalizer but no policy/PEP/tool.
"""
from __future__ import annotations
import argparse
from collections import Counter
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys

from archive_development_adapter import adapt, make_task, oracle, AdaptationError
from scout_public_workload import digest

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.connectors.builtin.git_webhook.normalizers import GitHubNormalizer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing overwrite')
    inventory_path = BASE / 'results/public-workload-scout-20260930-v2.json'
    inventory = json.loads(inventory_path.read_text(encoding='utf-8'))
    assert digest(inventory_path) == '5bf84fb6822d99b12f9160562f2678c66e03ee9e8d95766b945e6c094701a750'
    # Choose before adapting, no silently skipping malformed earliest examples.
    selected = []
    for kind in ('PushEvent', 'PullRequestEvent', 'IssuesEvent'):
        rows = [e for e in inventory['events'] if e['repository'] == 'microsoft/vscode'
                and e['event_type'] == kind and (kind == 'PushEvent' or e['action'] == 'opened')]
        selected.append(min(rows, key=lambda e: (e['created_at'], e['event_id'])))
    chosen_ids = {e['event_id'] for e in selected}
    examples, coverage = [], []
    for archive in inventory['archives']:
        wanted = {e['line']: e for e in inventory['events'] if e['archive_hour'] == archive['hour']}
        if not wanted:
            continue
        path = ROOT / 'tmp/rr022-gharchive-scout-v1' / (archive['hour'] + '.json.gz')
        assert digest(path) == archive['sha256']
        with gzip.open(path, 'rb') as src:
            for line, raw in enumerate(src, 1):
                if line not in wanted:
                    continue
                row = wanted[line]
                assert hashlib.sha256(raw).hexdigest() == row['raw_line_sha256']
                event = json.loads(raw)
                assert event['id'] == row['event_id']
                try:
                    adapted = adapt(event)
                except AdaptationError as exc:
                    coverage.append({'event_id': event['id'], 'status': 'unsupported', 'reason': str(exc)})
                    if event['id'] in chosen_ids:
                        raise
                    continue
                coverage.append({'event_id': event['id'], 'status': 'adaptable'})
                if event['id'] not in chosen_ids:
                    continue
                normalized = GitHubNormalizer().normalize({'x-github-event': adapted['normalizer_event_name']},
                                                         adapted['projected_payload'])
                assert normalized['type'] == adapted['expected_normalized_type']
                assert normalized['source'] == 'github/' + row['repository']
                # Replace generated identity/time with explicitly namespaced archived values.
                normalized['id'] = 'gharchive:' + event['id']
                normalized['time'] = event['created_at']
                normalized['data']['_archive_provenance'] = {'mode': 'offline_public_event_replay',
                    'archive_url': archive['url'], 'archive_sha256': archive['sha256'],
                    'line': line, 'raw_line_sha256': row['raw_line_sha256'],
                    'original_webhook_signature_verified': False}
                task = make_task(adapted)
                examples.append({'source_record': row, 'adaptation': adapted,
                                 'normalized_event': normalized, 'task': task})
    assert len(examples) == 3
    examples.sort(key=lambda e: ('push', 'pr', 'issue').index(e['adaptation']['target']['kind']))
    for example in examples:
        task = example['task']
        variants = [('normal', copy.deepcopy(task['call']))]
        source = example['source_record']
        # Use a different real object of the SAME kind, not a trivial PR->push swap.
        alternatives = [e for e in inventory['events'] if e['repository'] == source['repository']
                        and e['event_type'] == source['event_type'] and e['object_group']
                        and e['object_group'] != source['object_group']]
        other = min(alternatives, key=lambda e: (e['created_at'], e['event_id']))
        other_id = other['object_number'] if other['object_number'] is not None else other['object_group'].rsplit(':', 1)[1]
        example['different_object_provenance'] = other
        wrong_object = copy.deepcopy(task['call'])
        wrong_object['arguments']['resource'] = f"rr022-dev/{source['repository']}/{example['adaptation']['target']['kind']}/{other_id}"
        variants.append(('different_object', wrong_object))
        wrong_version = copy.deepcopy(task['call'])
        target = example['adaptation']['target']
        wrong_version['arguments']['version'] = target.get('before', target.get('base', '0' * 64))
        assert wrong_version['arguments']['version'] != task['policy']['version']
        variants.append(('different_version_or_snapshot', wrong_version))
        example['expected_cases'] = [{'name': name, 'call': call, 'expected': oracle(task['policy'], call),
                                      'expected_receipts': 1 if name == 'normal' else 0}
                                     for name, call in variants]
        assert [case['expected']['allow'] for case in example['expected_cases']] == [True, False, False]
    result = {'status': 'development_examples_built_not_governance_results',
              'selection': 'Earliest UTC time then event id per PushEvent/PR opened/Issue opened in microsoft/vscode; selected before adapting; no skip on failure.',
              'inventory_sha256': digest(inventory_path),
              'source_hashes': {p.name: digest(p) for p in (Path(__file__), BASE / 'archive_development_adapter.py',
                    ROOT / 'backend/app/connectors/builtin/git_webhook/normalizers.py')},
              'examples': examples, 'coverage': coverage,
              'coverage_summary': dict(Counter(c['status'] + ':' + c.get('reason', '') for c in coverage)),
              'boundary': 'Native normalizer exercised for three projected metadata events; no PEP, approval, task runtime, grant, actual tool or external baseline executed. Labels await independent review.'}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'examples': [{'kind': e['adaptation']['target']['kind'], 'event': e['source_record']['event_id'],
                                  'target': e['adaptation']['target'], 'expected': e['expected_cases']} for e in examples],
                      'coverage': result['coverage_summary']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
