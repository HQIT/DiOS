"""Service availability and read-response dependency controls, no governance."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from run_archive_workflow_pilot import content_service, post_record, report_arguments, ROOT, HERE, file_hash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('result', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    data = json.loads(args.result.read_text(encoding='utf-8'))
    controls = []
    for row in data['rows']:
        if row['method'] != 'e2ag' or not row['case'].endswith((':other_snapshot', ':other_report')):
            continue
        job = row['job']
        directory = Path(tempfile.mkdtemp(prefix='archive-workflow-control-', dir=ROOT / 'tmp/rr022-baselines'))
        with content_service(directory, job['catalog']) as (url, ledger, reports, operations):
            rpc = post_record(url, {'tool': 'snapshot.read', 'arguments': job['read_arguments']})
            returned = json.loads(rpc['result']['content'][0]['text'])
            candidate = report_arguments(job, returned)
            if row['case'].endswith(':other_report'):
                assert candidate['resource'] != job['policy_arguments']['report.write']['resource']
                post_record(url, {'tool': 'report.write', 'arguments': candidate})
                assert len(reports.read_text(encoding='utf-8').splitlines()) == 1
            else:
                assert job['read_arguments']['resource'] != job['policy_arguments']['snapshot.read']['resource']
                assert len(operations) == 1
            altered = {'content_base64': base64.b64encode(b'controlled content dependency\n').decode()}
            observed = report_arguments(job, altered)
            assert observed['content_sha256'] == hashlib.sha256(b'controlled content dependency\n').hexdigest()
            assert observed['byte_count'] == 30 and observed['line_count'] == 1
            assert observed['content_sha256'] != job['policy_arguments']['report.write']['content_sha256']
        controls.append({'case': row['case'], 'bypass_executed': True, 'read_dependency_checked': True,
                         'ledger': str(ledger), 'ledger_sha256': file_hash(ledger), 'operations': operations,
                         'reports_path': str(reports), 'reports_sha256': file_hash(reports)})
    assert len(controls) == 6
    result = {'status': 'passed', 'paired_result_sha256': file_hash(args.result),
              'checker_sha256': file_hash(Path(__file__)), 'controls': controls,
              'boundary': 'Synthetic response alteration tests dependency only; not a naturally observed error or security case.'}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'passed', 'controls': len(controls)}))


if __name__ == '__main__':
    main()
