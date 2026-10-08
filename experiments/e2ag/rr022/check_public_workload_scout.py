"""Independently re-read cached lines and verify the inventory, without networking."""
import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('report', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing overwrite')
    r = json.loads(args.report.read_text(encoding='utf-8'))
    assert r['status'] == 'complete'
    assert [a['hour'] for a in r['archives']] == r['manifest']['hours']
    assert len({e['event_id'] for e in r['events']}) == len(r['events'])
    assert all('body' not in e and 'actor' not in e for e in r['events'])
    path = ROOT / 'backend/app/connectors/builtin/git_webhook/normalizers.py'
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    mapping = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                   and node.target.id == 'GITHUB_EVENT_MAP')
    event_names = {'PushEvent': 'push', 'PullRequestEvent': 'pull_request', 'IssuesEvent': 'issues',
                   'IssueCommentEvent': 'issue_comment', 'PullRequestReviewEvent': 'pull_request_review',
                   'PullRequestReviewCommentEvent': 'pull_request_review_comment'}
    checked = 0
    foreign_hour_records = 0
    covered, gaps = defaultdict(Counter), defaultdict(Counter)
    details = []
    for archive in r['archives']:
        path = ROOT / 'tmp/rr022-gharchive-scout-v1' / (archive['hour'] + '.json.gz')
        assert sha(path) == archive['sha256']
        selected = {e['line']: e for e in r['events'] if e['archive_hour'] == archive['hour']}
        counts = Counter()
        with gzip.open(path, 'rb') as f:
            for line_number, raw in enumerate(f, 1):
                event = json.loads(raw)
                counts[event.get('type', '(absent)')] += 1
                if line_number not in selected:
                    continue
                row = selected[line_number]
                assert hashlib.sha256(raw).hexdigest() == row['raw_line_sha256']
                assert event['id'] == row['event_id']
                assert event['repo']['name'] == row['repository']
                assert event['type'] == row['event_type']
                stamp = event.get('created_at')
                if stamp:
                    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc)
                    event_hour = parsed.strftime('%Y-%m-%d-') + str(parsed.hour)
                    foreign_hour_records += event_hour != archive['hour']
                key = (event_names.get(event['type']), (event.get('payload') or {}).get('action') or '')
                normalized = mapping.get(key)
                if normalized:
                    covered[row['repository']][normalized] += 1
                else:
                    gaps[row['repository']][f'{key[0] or event["type"]}/{key[1]}'] += 1
                details.append({'event_id': event['id'], 'mapped_type': normalized})
                checked += 1
        assert line_number == archive['lines']
        assert dict(counts) == archive['all_event_types']
    assert checked == len(r['events'])
    for name, summary in r['summaries'].items():
        rows = [e for e in r['events'] if e['repository'].lower() == name.lower()]
        assert summary['events'] == len(rows)
        assert summary['unique_object_groups'] == len({e['object_group'] for e in rows if e['object_group']})
        assert summary['event_type_counts'] == dict(Counter(e['event_type'] for e in rows))
    result = {'status': 'passed', 'report_sha256': sha(args.report), 'checker_sha256': sha(Path(__file__)),
              'normalizer_sha256': sha(ROOT / 'backend/app/connectors/builtin/git_webhook/normalizers.py'),
              'archives_checked': len(r['archives']), 'archive_lines': sum(a['lines'] for a in r['archives']),
              'compressed_bytes': sum(a['compressed_bytes'] for a in r['archives']),
              'selected_lines_verified': checked, 'event_time_outside_archive_hour': foreign_hour_records,
              'explicit_normalizer_mapping': {k: dict(v) for k, v in covered.items()},
              'other_actions_or_types': {k: dict(v) for k, v in gaps.items()},
              'mapping_details': details,
              'boundary': 'Mapping availability only, not successful normalization/admission or historical signature verification. Out-of-hour events can reflect archive lag; not automatically corrupt.'}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'mapping_details'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
