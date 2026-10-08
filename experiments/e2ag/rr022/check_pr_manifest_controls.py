"""Real HTTP no-enforcement and deterministic read-dependency controls."""
import argparse
import base64
import copy
import json
from pathlib import Path
import tempfile
from run_archive_workflow_pilot import content_service, post_record
from run_paired_tool_pilot import ROOT, HERE, file_hash
from pr_manifest_workload import report_arguments


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('result', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    result = json.loads(args.result.read_text(encoding='utf-8'))
    controls = []
    for case in ('other_read_object', 'wrong_read_version', 'other_write_object'):
        row = next(r for r in result['rows'] if r['method'] == 'e2ag' and r['case'] == case)
        job = row['job']
        directory = Path(tempfile.mkdtemp(prefix='pr-no-gate-', dir=ROOT / 'tmp/rr022-baselines'))
        with content_service(directory, job['catalog']) as (url, ledger, reports, operations):
            response = post_record(url, {'tool': 'snapshot.read', 'arguments': job['read_arguments']})
            returned = json.loads(response['result']['content'][0]['text'])
            candidate = report_arguments(job, returned)
            if case == 'other_write_object':
                post_record(url, {'tool': 'report.write', 'arguments': candidate})
        disk = [json.loads(s) for s in ledger.read_text(encoding='utf-8').splitlines()]
        saved = [json.loads(s) for s in reports.read_text(encoding='utf-8').splitlines()]
        assert disk == operations and len(disk) == (2 if case == 'other_write_object' else 1)
        assert disk[0]['arguments'] == job['read_arguments']
        assert saved == ([candidate] if case == 'other_write_object' else [])
        controls.append(dict(case=case, passed=True, ledger=str(ledger), ledger_sha256=file_hash(ledger),
                             reports=str(reports), reports_sha256=file_hash(reports), operations=disk))
    row = next(r for r in result['rows'] if r['method'] == 'e2ag' and r['case'] == 'normal')
    bundle = json.loads(Path(row['job']['catalog'][0]['cache_path']).read_text(encoding='utf-8'))
    # Deliberately synthetic response, not an authentic new snapshot or bypass.
    bundle['after'] = copy.deepcopy(bundle['before'])
    returned = {'content_base64': base64.b64encode(json.dumps(bundle).encode()).decode()}
    changed = json.loads(report_arguments(row['job'], returned)['report'])
    assert changed['changes'] == [] and changed['counts'] == dict(added=0, deleted=0, modified=0, submodule_paths=0)
    assert changed != json.loads(row['observed']['reports'][0]['report'])
    output = dict(status='passed', result_sha256=file_hash(args.result),
                  source_sha256={str(p.relative_to(ROOT)): file_hash(p) for p in (
                      Path(__file__), HERE / 'pr_manifest_workload.py', HERE / 'run_archive_workflow_pilot.py')},
                  no_gate_controls=controls, synthetic_response_dependency_control=True,
                  boundary='Three HTTP bypass controls and one synthetic response dependency check. Not additional independent workload objects.')
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(status='passed', no_gate_controls=3, response_dependency_controls=1)))


if __name__ == '__main__':
    main()
