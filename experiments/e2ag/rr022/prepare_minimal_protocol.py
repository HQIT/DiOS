"""Offline preparation only: expanded exposure union and candidate checks."""
import argparse
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--exclusions', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists() and not args.exclusions.exists()
    path = HERE / 'public-workload-freeze-candidate-v0.4.json'
    candidate = json.loads(path.read_text(encoding='utf-8'))
    assert candidate['formal_execution_enabled'] is False and candidate['author_approval'] is None
    assert sum(s['maximum_objects'] for s in candidate['strata']) == candidate['comparison']['max_objects'] == 30
    assert 30 * len(candidate['comparison']['methods']) * len(candidate['comparison']['main_cases']) == 360
    ids, groups, heads, sources = set(), set(), set(), {}
    for name in candidate['exposure_sources']:
        source = HERE / name
        sources[name] = sha(source)
        value = json.loads(source.read_text(encoding='utf-8'))
        if 'exploration_exclusions' in value:
            exclusion = value['exploration_exclusions']
            ids.update(map(str, exclusion['event_ids']))
            groups.update(v.lower() for v in exclusion['object_groups'])
            heads.update(v.lower() for v in exclusion['head_shas'])
        else:
            for event in value['events']:
                ids.add(str(event['event_id']))
                if event.get('object_group'):
                    groups.add(event['object_group'].lower())
                for key in ('head', 'pr_head_sha'):
                    if event.get(key) and re.fullmatch('[0-9a-fA-F]{40}', event[key]):
                        heads.add(event[key].lower())
    assert len(ids) >= 32476 and len(groups) >= 32439 and len(heads) >= 25725
    feasibility_path = HERE / 'results/minimal-repository-feasibility-20260930-v1.json'
    feasibility = json.loads(feasibility_path.read_text(encoding='utf-8'))
    assert feasibility['status'] == 'checked'
    for project in feasibility['projects']:
        assert project['exposed_pr']['event_id'] in ids and project['exposed_pr']['object_group'] in groups and project['head'] in heads
    assert next(p for p in feasibility['projects'] if p['repository'] == 'arcadedata/arcadedb')['status'] == 'one_exposed_pr_feasible'
    for name in ('issue-intake-checks-20260930-v1.json', 'pr-manifest-checks-20260930-v1.json'):
        evidence = HERE / 'results' / name
        check = json.loads(evidence.read_text(encoding='utf-8'))
        assert check['status'] == 'passed'
        pilot_name = name.replace('-checks-', '-pilot-')
        pilot_path = HERE / 'results' / pilot_name
        assert sha(pilot_path) == check['result_sha256']
        pilot = json.loads(pilot_path.read_text(encoding='utf-8'))
        assert all(sha(ROOT / source) == checksum for source, checksum in pilot['source_sha256'].items())
        sources['results/' + name] = sha(evidence)
    known_hours = [p.stem.replace('.json', '') for p in (ROOT / 'tmp/rr022-gharchive-scout-v1').glob('*.json.gz')]
    assert not any(hour[:10] in ('2026-09-22', '2026-09-23', '2026-09-24') for hour in known_hours)
    exclusion = dict(status='exposure_union_not_sample', source_sha256=sources, excluded_dates=candidate['exploration_dates'],
                     event_ids=sorted(ids), object_groups=sorted(groups), head_shas=sorted(heads))
    args.exclusions.write_text(json.dumps(exclusion, indent=2) + '\n', encoding='utf-8')
    output = dict(status='offline_candidate_checks_passed_not_author_approval', candidate_sha256=sha(path),
                  implementation_sha256=sha(Path(__file__)), feasibility_sha256=sha(feasibility_path),
                  exclusion_path=str(args.exclusions.resolve()), exclusion_sha256=sha(args.exclusions),
                  counts=dict(event_ids=len(ids), object_groups=len(groups), head_shas=len(heads)),
                  max_objects=30, max_paired_runs=360, formal_runs=0,
                  boundary='Known cache only, not proof that nobody has seen target data. No formal collection or execution.')
    args.output.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(output))


if __name__ == '__main__':
    main()
