"""Freeze queue qualification evidence without changing earlier experiment records."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from performance_pretrial import ROOT, HERE, dump, file_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    predecessor_path = HERE / 'results/transport-reuse-manifest-20260930-v1.json'
    predecessor = json.loads(predecessor_path.read_text(encoding='utf-8'))
    retained = {p: h for p, h in predecessor['current_source_sha256'].items() if p.startswith('backend/')}
    for path, digest in retained.items():
        assert file_hash(ROOT / path) == digest, path
    names = ['queued-storage-faults-20261001-v2.json', 'queued-storage-process-check-20261001-v1.json',
             'queued-storage-matrix-20261001-v1.json', 'queued-storage-matrix-check-20261001-v1.json',
             'queued-storage-matrix-failures-20261001-v1.json', 'queued-storage-matrix-analysis-20261001-v1.json']
    for name in names[:3]:
        data = json.loads((HERE / 'results' / name).read_text(encoding='utf-8'))
        assert data['status'] != 'running'
        assert data['db_admission'] == 'queued'
        for path, digest in data['source_sha256'].items():
            assert file_hash(ROOT / path) == digest, path
        for row in data['rows']:
            cell = row.get('cell', row)
            assert cell['verification']['verified'], cell['directory']
            assert cell['storage_runtime']['pool_activity'] == {'checked_out': 0, 'peak_checked_out': 1}
    paths = ['performance_pretrial.py', 'storage_runtime.py', 'test_storage_runtime.py',
             'check_persistence_faults.py', 'test_performance_pretrial.py', 'record_queued_storage.py',
             'check_performance_pretrial.py', 'check_performance_failures.py', 'analyze_transport_reuse.py']
    matrix = json.loads((HERE / 'results' / names[2]).read_text(encoding='utf-8'))
    result = dict(status='queue_qualification_recorded_not_formal_performance',
                  recorded_at=datetime.now(timezone.utc).isoformat(),
                  predecessor=str(predecessor_path.relative_to(ROOT)), predecessor_sha256=file_hash(predecessor_path),
                  unchanged_backend_source_sha256=retained,
                  current_source_sha256={str((HERE / p).relative_to(ROOT)): file_hash(HERE / p) for p in paths},
                  evidence_sha256={name: file_hash(HERE / 'results' / name) for name in names},
                  protocol_sha256=file_hash(ROOT / 'docs/paper/revision-rr022/queued-storage-protocol-20261001.md'),
                  matrix_status=matrix['status'], checked_cells=len(matrix['rows']),
                  incomplete_fault_v1_preserved='queued-storage-faults-20261001-v1.json',
                  pre_edit_backup='tmp/transaction-queue-before-20261001-v1',
                  production_configuration_changed=False, remotely_deployed=False, holdout_method_runs=0,
                  boundary='Experiment-only single-connection async pool, identical across G0/G1/G2. All transactions queued; wait included in latency. No SQLite novelty, production capacity, or exactly-once claim.')
    dump(args.output, result)
    print(json.dumps(dict(status=result['status'], checked_cells=len(matrix['rows']), unchanged_backend_files=len(retained))))


if __name__ == '__main__':
    main()
