"""Whole exploration-day availability screen, no network or method execution."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
from audit_workload_selection import inspect_input, digest, BASE, ROOT


def rank_repositories(counts, manifest):
    threshold = manifest['minimum_unique_objects_each_workflow']
    excluded = {v.lower() for v in manifest['excluded_development_repositories']}
    eligible = [name for name, c in counts.items() if name not in excluded
                and c.get('issue_intake', 0) >= threshold and c.get('pr_change_manifest', 0) >= threshold]
    def key(name):
        return hashlib.sha256((manifest['seed'] + '\n' + name).encode()).hexdigest(), name
    return sorted(eligible, key=key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing overwrite')
    mp = BASE / 'workload-candidate-pool-screen-v1.json'
    m = json.loads(mp.read_text(encoding='utf-8'))
    source = BASE / m['source']
    inventory = json.loads(source.read_text(encoding='utf-8'))
    assert inventory['status'] == 'complete' and len(inventory['archives']) == 24
    expected_hours = {f'2026-09-21-{h}' for h in range(24)}
    assert {a['hour'] for a in inventory['archives']} == expected_hours
    counts, records, seen, statuses = defaultdict(lambda: defaultdict(set)), [], set(), Counter()
    seen_objects, seen_heads = set(), set()
    hours = []
    for archive in inventory['archives']:
        path = ROOT / 'tmp/rr022-gharchive-scout-v1' / (archive['hour'] + '.json.gz')
        assert digest(path) == archive['sha256']
        candidates = 0
        with gzip.open(path, 'rb') as stream:
            for number, raw in enumerate(stream, 1):
                event = json.loads(raw)
                if event.get('type') not in ('IssuesEvent', 'PullRequestEvent') or (event.get('payload') or {}).get('action') != 'opened':
                    continue
                candidates += 1
                eid = str(event.get('id'))
                if eid in seen:
                    statuses['duplicate_event_id'] += 1
                    continue
                seen.add(eid)
                check = inspect_input(event)
                statuses[check['status']] += 1
                if not check['status'].startswith('metadata_eligible_'):
                    continue
                repo = event['repo']['name'].lower()
                counts[repo][check['workflow']].add(check['object_group'])
                seen_objects.add(check['object_group'])
                if check.get('head_sha'):
                    seen_heads.add(check['head_sha'])
                records.append({'event_id': eid, 'repository': repo, 'object_group': check['object_group'],
                                'workflow': check['workflow'], 'head_sha': check.get('head_sha'),
                                'archive_hour': archive['hour'], 'line': number,
                                'raw_line_sha256': hashlib.sha256(raw).hexdigest()})
        assert number == archive['lines']
        hours.append({'hour': archive['hour'], 'sha256': archive['sha256'], 'lines': number, 'target_opened_records': candidates})
        print(json.dumps(hours[-1]), flush=True)
    count_table = {repo: {w: len(groups) for w, groups in workflows.items()} for repo, workflows in counts.items()}
    ranked = rank_repositories(count_table, m)
    selected = ranked[:m['shortlist_limit']]
    result = {'status': 'exploration_screen_complete_not_repository_approval', 'manifest_sha256': digest(mp),
              'inventory_sha256': digest(source), 'implementation_sha256': {p.name: digest(p) for p in
                  (Path(__file__), BASE / 'audit_workload_selection.py', BASE / 'archive_development_adapter.py')},
              'hours': hours, 'status_counts': dict(statuses), 'repositories_with_eligible_metadata': len(count_table),
              'repositories_passing_two_workflow_threshold': len(ranked),
              'shortlist': [{'repository': r, **count_table[r]} for r in selected],
              'all_ranked_passing_repositories': ranked, 'metadata_counts': count_table,
              'exploration_exclusions': {'event_ids': sorted(seen), 'object_groups': sorted(seen_objects), 'head_shas': sorted(seen_heads)},
              'eligible_records': records, 'formal_runs': 0,
              'boundary': 'Source availability only, not task success. Original/fork status and historical trees unverified. Full Sep21 plus listed objects/heads excluded from future holdout.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('status', 'repositories_with_eligible_metadata', 'repositories_passing_two_workflow_threshold', 'shortlist')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
