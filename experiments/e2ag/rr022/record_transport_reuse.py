"""Record transport intervention without replacing prior freezes or results."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from run_paired_tool_pilot import ROOT, HERE


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    predecessor_path = HERE/'results/persistence-repair-manifest-20260930-v1.json'
    predecessor = json.loads(predecessor_path.read_text(encoding='utf-8'))
    retained = ['backend/app/services/e2ag_tool_gateway.py', 'backend/app/models/tables.py', 'backend/app/db/database.py',
                'backend/tests/test_e2ag_invocation_persistence.py']
    for path in retained:
        assert sha(ROOT/path) == predecessor['current_source_sha256'][path], path
    original = json.loads((HERE/'results/approved-protocol-freeze-20260930-v1.json').read_text(encoding='utf-8'))
    unchanged = []
    for path, old in original['source_sha256'].items():
        path = path.replace('\\', '/')
        if path in {'backend/app/api/internal/e2ag_mcp.py', 'backend/app/services/e2ag_tool_gateway.py'}:
            continue
        assert sha(ROOT/path) == old, path
        unchanged.append(path)
    names = ['transport-reuse-faults-20260930-v1.json', 'transport-reuse-pair-20260930-v1.json',
             'transport-reuse-pair-check-20260930-v1.json', 'transport-reuse-pair-analysis-20260930-v1.json',
             'transport-reuse-pair-failures-20260930-v2.json', 'transport-reuse-matrix-20260930-v1.json',
             'transport-reuse-matrix-check-20260930-v1.json', 'transport-reuse-matrix-failures-20260930-v1.json',
             'transport-reuse-matrix-analysis-20260930-v1.json']
    for name in ('transport-reuse-pair-20260930-v1.json', 'transport-reuse-matrix-20260930-v1.json'):
        result = json.loads((HERE/'results'/name).read_text(encoding='utf-8'))
        assert result['status'] != 'running'
        for path, digest in result['source_sha256'].items():
            assert sha(ROOT/path) == digest, path
    paths = retained + ['backend/app/api/internal/e2ag_mcp.py', 'backend/app/services/e2ag_http_transport.py',
                        'backend/main.py', 'backend/app/config.py', 'backend/tests/test_e2ag_http_transport.py',
                        'experiments/e2ag/rr022/performance_pretrial.py', 'experiments/e2ag/rr022/run_transport_reuse_pair.py',
                        'experiments/e2ag/rr022/analyze_transport_reuse.py', 'experiments/e2ag/rr022/check_performance_failures.py',
                        'experiments/e2ag/rr022/check_performance_pretrial.py', 'experiments/e2ag/rr022/record_transport_reuse.py',
                        'docs/paper/revision-rr022/transport-reuse-protocol-20260930.md']
    result = dict(status='transport_intervention_recorded_not_formal_performance', recorded_at=datetime.now(timezone.utc).isoformat(),
                  predecessor=str(predecessor_path.relative_to(ROOT)), predecessor_sha256=sha(predecessor_path),
                  unchanged_original_frozen_sources=unchanged, unchanged_persistence_implementation=retained,
                  current_source_sha256={path:sha(ROOT/path) for path in paths},
                  evidence_sha256={name:sha(HERE/'results'/name) for name in names},
                  pre_edit_backup='tmp/client-reuse-before-20260930-221036',
                  production_default_enabled=False, remotely_deployed=False, holdout_method_runs=0,
                  boundary='Only transport lifecycle changed. Authorization, persistence ordering, SQLite settings and resource stop thresholds retained; no production performance or full reliability claim.')
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(status=result['status'], unchanged_original_sources=len(unchanged), evidence_files=len(names))))


if __name__ == '__main__':
    main()
