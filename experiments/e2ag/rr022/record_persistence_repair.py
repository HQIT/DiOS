"""Record a transparent successor to the collection freeze; do not replace it."""
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
    predecessor_path = HERE / 'results/approved-protocol-freeze-20260930-v1.json'
    predecessor = json.loads(predecessor_path.read_text(encoding='utf-8'))
    comparisons = []
    for name, old in predecessor['source_sha256'].items():
        name = name.replace('\\', '/')
        new = sha(ROOT / name)
        comparisons.append(dict(path=name, before=old, after=new, changed=old != new))
    changed = {row['path'] for row in comparisons if row['changed']}
    assert changed == {'backend/app/api/internal/e2ag_mcp.py', 'backend/app/services/e2ag_tool_gateway.py'}, changed
    paths = ['backend/app/api/internal/e2ag_mcp.py', 'backend/app/services/e2ag_tool_gateway.py',
             'backend/app/models/tables.py', 'backend/app/db/database.py',
             'backend/tests/test_e2ag_invocation_persistence.py', 'experiments/e2ag/rr022/performance_pretrial.py',
             'experiments/e2ag/rr022/check_persistence_faults.py', 'experiments/e2ag/rr022/check_performance_failures.py',
             'experiments/e2ag/rr022/record_persistence_repair.py']
    backup = ROOT / 'tmp/persistence-before-20260930-215026'
    backups = []
    for path in paths:
        old = backup / path.replace('/', '__')
        if old.exists():
            backups.append(dict(path=path, backup_sha256=sha(old), current_sha256=sha(ROOT / path)))
    evidence_names = ['persistence-socket-faults-20260930-v2.json', 'performance-persistence-process-check-20260930-v1.json',
                      'performance-persistence-pretrial-20260930-v1.json', 'performance-persistence-check-20260930-v1.json',
                      'performance-persistence-failures-20260930-v1.json']
    evidence = {name:sha(HERE / 'results' / name) for name in evidence_names}
    pretrial = json.loads((HERE / 'results/performance-persistence-pretrial-20260930-v1.json').read_text(encoding='utf-8'))
    for path, expected in pretrial['source_sha256'].items():
        assert sha(ROOT / path) == expected, f'Source changed during pretrial: {path}'
    result = dict(status='reliability_repair_recorded_concurrent_availability_not_passed', recorded_at=datetime.now(timezone.utc).isoformat(),
                  predecessor=str(predecessor_path.relative_to(ROOT)), predecessor_sha256=sha(predecessor_path),
                  source_comparisons=comparisons, current_source_sha256={path:sha(ROOT / path) for path in paths},
                  pre_edit_backup=str(backup.relative_to(ROOT)), backup_comparisons=backups, evidence_sha256=evidence,
                  policy_and_workload_unchanged=True, holdout_method_runs=0, formal_performance=False,
                  connection_pooling_changed=False, production_deployed=False,
                  boundary='Development-only durability repair. Existing collection freeze is retained, not silently replaced. Formal comparative execution still waits for independent review and final method refreeze.')
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(changed_frozen_sources=sorted(changed), unchanged_frozen_sources=len(comparisons)-len(changed),
                          status=result['status'])))


if __name__ == '__main__':
    main()
