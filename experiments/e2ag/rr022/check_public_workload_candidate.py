"""Offline proposal preflight; never downloads, samples or runs a method."""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(m):
    if m['status'] != 'candidate_not_frozen' or m['author_approval'] is not None or m['formal_execution_enabled'] is not False:
        raise ValueError('This preflight cannot approve or freeze a protocol')
    start = datetime.fromisoformat(m['window']['start_utc'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(m['window']['end_utc_exclusive'].replace('Z', '+00:00'))
    if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0) or end <= start:
        raise ValueError('Invalid UTC window')
    if any((v.hour, v.minute, v.second, v.microsecond) != (0, 0, 0, 0) for v in (start, end)):
        raise ValueError('Window must use whole UTC days')
    hours = int((end - start).total_seconds() / 3600)
    if hours != m['window']['hours'] or hours != 72:
        raise ValueError('Window-hour mismatch')
    names = [n.lower() for n in m['holdout_repositories']]
    if len(set(names)) != len(names) or set(names) & {n.lower() for n in m['development_repositories']}:
        raise ValueError('Repository leakage or duplicate')
    dates = {(start + timedelta(hours=i)).date().isoformat() for i in range(hours)}
    if dates & set(m['exploration_dates']):
        raise ValueError('Exploration-date leakage')
    selection, comparison = m['selection'], m['comparison']
    maximum = len(names) * len(selection['workflows']) * selection['max_objects_per_repository_per_workflow']
    if maximum != selection['max_objects'] or maximum != 100:
        raise ValueError('Object cap mismatch')
    runs = maximum * len(comparison['main_cases']) * len(comparison['methods'])
    if runs != comparison['max_paired_runs'] or runs != m['limits']['max_paired_runs']:
        raise ValueError('Paired run cap mismatch')
    if not comparison['shared_agentspec_admission'] or comparison['native_agent_completion_claim'] or comparison['performance_claim']:
        raise ValueError('Misleading shared-host or evidence claim')
    if selection['outcome_based_selection'] or m['review']['answers_prefilled'] or not m['review']['blinded_to_method_and_outcome']:
        raise ValueError('Outcome leakage into selection or review')
    expected_gates = {'author_protocol_approval', 'development_business_workflows_passed', 'source_and_policy_hashes_frozen',
                      'all_72_archive_hours_complete', 'seen_object_exclusion_manifest_written'}
    if not expected_gates <= set(m['required_before_sampling']):
        raise ValueError('Missing sampling gate')
    return [(start + timedelta(hours=i)).strftime('%Y-%m-%d-') + str((start + timedelta(hours=i)).hour) for i in range(hours)]


def exclusion_sets(reports):
    ids, groups, heads = set(), set(), set()
    for report in reports:
        if report['status'] != 'complete':
            raise ValueError('Incomplete exploration inventory')
        for event in report['events']:
            ids.add(str(event['event_id']))
            if event.get('object_group'):
                groups.add(event['object_group'].lower())
            for key in ('head', 'pr_head_sha'):
                if event.get(key):
                    heads.add(event[key].lower())
    # Base/before SHAs are not excluded: sharing an ancestor is not sample reuse.
    return {'event_ids': sorted(ids), 'object_groups': sorted(groups), 'head_shas': sorted(heads)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', type=Path, default=BASE / 'public-workload-freeze-candidate-v0.2.json')
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite existing preflight')
    m = json.loads(args.manifest.read_text(encoding='utf-8'))
    hours = validate(m)
    sources = [BASE / p for p in m['exploration_results']]
    reports = [json.loads(p.read_text(encoding='utf-8')) for p in sources]
    for report in reports:
        observed_dates = {a['hour'][:10] for a in report['archives']}
        if not observed_dates <= set(m['exploration_dates']):
            raise ValueError('Manifest omits an explored date')
    excluded = exclusion_sets(reports)
    cache = ROOT / 'tmp/rr022-gharchive-scout-v1'
    overlaps = [h for h in hours if (cache / (h + '.json.gz')).exists()]
    result = {'status': 'proposal_preflight_passed_not_frozen', 'manifest_sha256': checksum(args.manifest),
              'checker_sha256': checksum(Path(__file__)),
              'exploration_results_sha256': {str(p.relative_to(BASE)): checksum(p) for p in sources},
              'planned_hours': hours, 'holdout_files_already_in_known_cache': overlaps,
              'exclusion_counts': {k: len(v) for k, v in excluded.items()}, 'exclusions': excluded,
              'max_objects': m['selection']['max_objects'], 'max_paired_runs': m['comparison']['max_paired_runs'],
              'formal_runs_executed': 0,
              'limitation': 'Known inventory/cache audit only, not proof of absence from all prior human or agent exposure. New business workflows and approval still pending.'}
    if overlaps:
        raise ValueError('Proposed holdout data already cached: inspect exposure before proceeding')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('status', 'exclusion_counts', 'max_objects', 'max_paired_runs', 'formal_runs_executed')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
