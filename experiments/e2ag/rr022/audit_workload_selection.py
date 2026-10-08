"""Recheck already explored archives against the proposed business inputs.

Offline only; no governance result, model, new window or historical API calls.
Export field availability/counts, never issue bodies, titles or actor identities.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

from archive_development_adapter import adapt, AdaptationError

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def inspect_input(event):
    kind, payload = event.get('type'), event.get('payload') or {}
    if kind not in ('IssuesEvent', 'PullRequestEvent'):
        return {'status': 'outside_target_event_types'}
    if payload.get('action') != 'opened':
        return {'status': 'outside_target_actions'}
    try:
        adapted = adapt(event)
    except AdaptationError as exc:
        return {'status': 'invalid_identity_or_version', 'reason': str(exc)}
    target = adapted['target']
    common = {'workflow': 'issue_intake' if kind == 'IssuesEvent' else 'pr_change_manifest',
              'object_group': f"{target['repository'].lower()}:{target['kind']}:{target['number']}",
              'version': adapted['version']}
    if kind == 'PullRequestEvent':
        return {**common, 'status': 'metadata_eligible_history_unverified', 'base_sha': target['base'],
                'head_sha': target['head'], 'historical_content_checked': False}
    issue = payload['issue']
    absent = sorted({'title', 'body', 'labels'} - set(issue))
    if absent:
        return {**common, 'status': 'missing_business_fields', 'missing_fields': absent}
    labels = issue['labels']
    if (not isinstance(issue['title'], str) or issue['body'] is not None and not isinstance(issue['body'], str)
            or not isinstance(labels, list) or any(not isinstance(v, dict) or not isinstance(v.get('name'), str) for v in labels)):
        return {**common, 'status': 'invalid_business_field_types'}
    body = issue['body']
    return {**common, 'status': 'metadata_eligible_archived_content_present',
            'body_state': 'null' if body is None else 'blank' if not body.strip() else 'nonempty',
            'body_characters': len(body or ''), 'label_count': len(labels),
            'historical_content_checked': True}


def summarize(rows):
    eligible = [r for r in rows if r['inspection']['status'].startswith('metadata_eligible_')]
    return {'events': len(rows), 'status_counts': dict(sorted(Counter(r['inspection']['status'] for r in rows).items())),
            'issue_objects': len({r['inspection']['object_group'] for r in eligible if r['inspection']['workflow'] == 'issue_intake'}),
            'pr_objects_pending_history': len({r['inspection']['object_group'] for r in eligible if r['inspection']['workflow'] == 'pr_change_manifest'}),
            'unique_pr_heads': len({r['inspection']['head_sha'] for r in eligible if r['inspection']['workflow'] == 'pr_change_manifest'})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite an existing audit')
    paths = [BASE / f'results/public-workload-scout-20260930-v{i}.json' for i in (1, 2)]
    inventories = [json.loads(p.read_text(encoding='utf-8')) for p in paths]
    archives, events = {}, {}
    for inv in inventories:
        assert inv['status'] == 'complete'
        for archive in inv['archives']:
            old = archives.setdefault(archive['hour'], archive)
            assert old['sha256'] == archive['sha256']
        for event in inv['events']:
            old = events.setdefault(str(event['event_id']), event)
            assert old == event
    rows = []
    for hour, archive in sorted(archives.items()):
        path = ROOT / 'tmp/rr022-gharchive-scout-v1' / (hour + '.json.gz')
        assert digest(path) == archive['sha256']
        wanted = {e['line']: e for e in events.values() if e['archive_hour'] == hour}
        if not wanted:
            continue
        with gzip.open(path, 'rb') as stream:
            for line_number, raw in enumerate(stream, 1):
                if line_number not in wanted:
                    continue
                source = wanted[line_number]
                assert hashlib.sha256(raw).hexdigest() == source['raw_line_sha256']
                event = json.loads(raw)
                assert event['id'] == source['event_id'] and event['repo']['name'] == source['repository']
                rows.append({'event_id': event['id'], 'repository': source['repository'], 'archive_hour': hour,
                             'line': line_number, 'raw_line_sha256': source['raw_line_sha256'],
                             'inspection': inspect_input(event)})
    assert len(rows) == len(events)
    repositories = [r['name'] for r in inventories[1]['manifest']['repositories']]
    summary = {name: summarize([r for r in rows if r['repository'].lower() == name.lower()]) for name in repositories}
    by_date = {date: {name: summarize([r for r in rows if r['repository'].lower() == name.lower() and r['archive_hour'].startswith(date)])
                     for name in repositories} for date in sorted({h[:10] for h in archives})}
    result = {'status': 'exploration_input_audit_complete_not_formal_selection',
              'source_sha256': {str(p.relative_to(BASE)): digest(p) for p in paths},
              'implementation_sha256': {p.name: digest(p) for p in (Path(__file__), BASE / 'archive_development_adapter.py')},
              'archive_hours': sorted(archives), 'events_rechecked': len(rows), 'summary': summary,
              'by_date': by_date, 'rows': rows,
              'boundary': 'Sep21 is a full day, Sep28 only four hours. Issue eligibility checks input fields only; PR trees/history and both new business workflows are not validated. No governance verdicts used.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'events': len(rows), 'summary': summary}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
