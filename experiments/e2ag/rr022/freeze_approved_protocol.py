"""Record current author's approval and hash the pre-collection implementation."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from scout_public_workload import digest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    sources = {}
    for name in ('issue-intake-pilot-20260930-v1.json', 'pr-manifest-pilot-20260930-v1.json'):
        previous = json.loads((HERE / 'results' / name).read_text(encoding='utf-8'))
        for source, checksum in previous['source_sha256'].items():
            assert digest(ROOT / source) == checksum
            sources[source] = checksum
    for path in [Path(__file__), *(HERE / name for name in ('collect_holdout_archives.py', 'holdout_selection.py',
                 'test_holdout_selection.py', 'scout_public_workload.py', 'audit_workload_selection.py', 'archive_development_adapter.py')),
                 ROOT / 'backend/app/connectors/builtin/git_webhook/normalizers.py']:
        sources[str(path.relative_to(ROOT))] = digest(path)
    protocol = 'public-workload-freeze-candidate-v0.4.json'
    exclusions = 'results/holdout-exposure-union-20260930-v1.json'
    output = dict(status='collection_protocol_frozen_methods_wait_for_review', frozen_at=datetime.now(timezone.utc).isoformat(),
                  approval=dict(source='current human user conversation', text='没问题，继续吧',
                                context='Affirmative continuation after minimum protocol v0.4 and independent-review explanation'),
                  collection_authorized=True, method_execution_authorized=False,
                  protocol_path=protocol, protocol_sha256=digest(HERE / protocol),
                  exclusion_path=exclusions, exclusion_sha256=digest(HERE / exclusions), source_sha256=sources,
                  boundary='Source acquisition/selection and business policy fixed before collecting the window. Generic execution/review packaging must be hash-frozen again before comparative execution; no label or policy tuning on holdout.')
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(status=output['status'], freeze_sha256=digest(args.output), source_files=len(sources))))


if __name__ == '__main__': main()
