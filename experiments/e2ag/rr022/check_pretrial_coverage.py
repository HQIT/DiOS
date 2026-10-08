"""Check qualification coverage across separately preserved executions, not pool timings."""
import argparse
import json
from pathlib import Path

from performance_pretrial import ROOT, dump, file_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    sources = [(p, json.loads(p.read_text(encoding='utf-8'))) for p in args.inputs]
    original = sources[0][1]
    expected = [tuple(c) for c in original['planned_order']]
    selected, exclusions, executions = {}, [], []
    for path, source in sources:
        assert source['status'] != 'running'
        for key in ('warmup_seconds', 'measurement_seconds', 'proxy_transport', 'db_admission', 'environment'):
            assert source[key] == original[key], key
        for name, digest in source['source_sha256'].items():
            assert file_hash(ROOT / name) == digest, name
        measured = warm = failures = 0
        for cell in source['rows']:
            key = (cell['kind'], cell['mode'], cell['concurrency'])
            assert key in expected
            directory = Path(cell['directory'])
            raw = json.loads((directory / 'client-raw.json').read_text(encoding='utf-8'))
            assert file_hash(directory / 'client-raw.json') == cell['raw_sha256']
            for name, digest in cell['verification']['evidence_sha256'].items():
                assert file_hash(directory / name) == digest
            measured += sum(r['phase'] == 'measurement' for r in raw['records'])
            warm += sum(r['phase'] == 'warmup' for r in raw['records'])
            errors = sum(not r['success'] for r in raw['records'])
            failures += errors
            ref = dict(cell=list(key), source=str(path), directory=str(directory))
            if cell['status'] == 'pretrial_checked' and cell['verification']['verified'] and not errors:
                assert key not in selected, 'Duplicate valid cell: explicit independent-round handling required'
                assert cell['storage_runtime']['pool_activity'] == {'checked_out': 0, 'peak_checked_out': 1}
                selected[key] = ref
            else:
                exclusions.append(dict(**ref, stop_reason=cell.get('stop_reason'), status=cell['status'], all_phase_errors=errors))
        executions.append(dict(source=str(path), sha256=file_hash(path), status=source['status'],
                               measured_attempts=measured, warmup_attempts=warm, all_phase_errors=failures))
    missing = [list(c) for c in expected if c not in selected]
    result = dict(status='all_planned_cells_qualified' if not missing else 'qualification_incomplete',
                  executions=executions, planned_cells=len(expected), qualified_cells=len(selected),
                  selected=[selected[c] for c in expected if c in selected], missing=missing, excluded_but_preserved=exclusions,
                  boundary='Coverage only. Separate executions remain separate; no pooled latency, formal round, capacity, or external validity claim.')
    dump(args.output, result)
    print(json.dumps({k: result[k] for k in ('status', 'planned_cells', 'qualified_cells', 'missing')}))


if __name__ == '__main__':
    main()
