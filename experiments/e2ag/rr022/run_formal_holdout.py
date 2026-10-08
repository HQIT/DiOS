"""Approved fixed 11-object comparison; freeze before execution, retain all rows."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import copy
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'
sys.dont_write_bytecode = True
from check_formal_holdout import HERE, ROOT, RESULTS, INPUTS, ORACLE, read, sha, preflight, expected_report, check_row
from run_parameter_pilot import prepare_parser
from run_paired_tool_pilot import UPSTREAM, PIN

METHODS = ('e2ag', 'opa', 'agentspec_host')
QUALIFY = RESULTS / 'formal-development-qualification-20261007-v1.json'
RECHECK = RESULTS / 'formal-development-recheck-20261007-v2.json'
FREEZE = RESULTS / 'formal-execution-freeze-20261007-v1.json'
AMENDMENT = ROOT / 'docs/paper/revision-rr022/formal-execution-amendment-20261007.md'
PYTHON = ROOT / 'backend/.venv/Scripts/python.exe'


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')


def invoke(kind, job, method, folder):
    script = HERE / ('run_issue_intake_pilot.py' if kind == 'issue' else 'run_pr_manifest_pilot.py')
    command = [str(PYTHON), str(script), '--worker', method, '--parser', str(folder)]
    try:
        proc = subprocess.run(command, input=json.dumps(job), capture_output=True, text=True, encoding='utf-8', timeout=120, cwd=ROOT)
        try:
            observed = json.loads(proc.stdout)
        except ValueError:
            observed = dict(error='Invalid worker output', stdout=proc.stdout)
        return dict(observed=observed, exit_code=proc.returncode, stderr=proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return dict(observed=dict(error='Worker timeout', stdout=str(exc.stdout)), exit_code=None, stderr=str(exc.stderr))


def qualify():
    assert not QUALIFY.exists()
    from run_issue_intake_pilot import jobs as issue_jobs
    from run_pr_manifest_pilot import jobs as pr_jobs
    pre = preflight()
    folder, generation = prepare_parser()
    sources = source_hashes()
    result = dict(status='running', started_utc=now(), parser_folder=str(folder), parser_generation=generation,
                  source_sha256=sources, preflight=pre, rows=[])
    issue_snapshot = next(s for s in read(RESULTS / 'archive-snapshots-20260930-v1.json')['snapshots'] if s['kind'] == 'issue')
    pr_snapshot = read(RESULTS / 'pr-manifest-snapshot-20260930-v1.json')['snapshot']
    for kind, jobs, snapshot in [('issue', issue_jobs(), issue_snapshot), ('pr', pr_jobs(pr_snapshot), pr_snapshot)]:
        raw = Path(snapshot['cache_path']).read_bytes()
        wanted = expected_report(kind, raw)
        for case, job, _ in jobs:
            if case not in ('normal', 'other_read_object', 'wrong_read_version', 'other_write_object', 'undeclared_event', 'wrong_content_control'):
                continue
            for method in METHODS:
                row = dict(kind=kind, case=case, method=method, job=job, **invoke(kind, job, method, folder))
                try:
                    row['checked'] = check_row(row, raw, wanted)
                    row['pass'] = True
                except Exception:
                    row.update(pass_=False, check_error=traceback.format_exc())
                    row['pass'] = False
                result['rows'].append(row)
                save(QUALIFY, result)
                print(json.dumps(dict(kind=kind, case=case, method=method, passed=row['pass'], error=row.get('check_error'))), flush=True)
    assert source_hashes() == sources
    result.update(status='passed' if all(r['pass'] for r in result['rows']) else 'failed', finished_utc=now())
    save(QUALIFY, result)
    assert result['status'] == 'passed'


def source_hashes():
    paths = list((ROOT / 'backend/app').rglob('*.py')) + list(HERE.glob('*.py')) + list(HERE.glob('*.rego'))
    paths += list((UPSTREAM / 'src').rglob('*.py')) + list((UPSTREAM / 'src').rglob('*.g4'))
    paths += [UPSTREAM / 'src/spec_lang/antlr-4.13.2-complete.jar', AMENDMENT,
              ROOT / 'tmp/external-baselines/opa-v1.17.0/opa_linux_amd64_static']
    return {p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in sorted(set(paths))}


def freeze():
    assert not FREEZE.exists()
    qualification = read(QUALIFY)
    recheck = read(RECHECK)
    assert recheck['status'] == 'passed' and len(qualification['rows']) == 36
    assert recheck['qualification_sha256'] == sha(QUALIFY.read_bytes())
    sources = source_hashes()
    assert sources == recheck['source_sha256']
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=UPSTREAM, text=True).strip() == PIN
    output = dict(status='frozen_before_formal_execution', frozen_utc=now(), method_execution_authorized=True,
        approval='Author confirmed automatic independent-implementation verification in current conversation on 2026-10-07.',
        amendment_path=AMENDMENT.relative_to(ROOT).as_posix(), amendment_sha256=sha(AMENDMENT.read_bytes()),
        prior_protocol_freeze_sha256=sha((RESULTS / 'approved-protocol-freeze-20260930-v1.json').read_bytes()),
        source_sha256=sources, inputs_sha256=sha(INPUTS.read_bytes()), oracle_sha256=sha(ORACLE.read_bytes()),
        qualification_sha256=sha(QUALIFY.read_bytes()), recheck_sha256=sha(RECHECK.read_bytes()), parser_folder=qualification['parser_folder'],
        parser_generation=qualification['parser_generation'], upstream_commit=PIN,
        dependencies={d.metadata['Name']: d.version for d in importlib.metadata.distributions()},
        agentspec_dependencies=subprocess.check_output([str(ROOT / 'tmp/rr022-baselines/.venv-agentspec/Scripts/python.exe'),
                                                       '-c', 'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"]:d.version for d in m.distributions()}))'], text=True),
        main_runs=132, diagnostic_runs=18, ungated_requests=33, human_review_required=False, preflight=preflight())
    save(FREEZE, output)
    print(json.dumps(dict(status=output['status'], sources=len(sources), sha256=sha(FREEZE.read_bytes()))))


def planned(inputs):
    seen = set()
    for obj in inputs['objects']:
        for case in obj['cases']:
            yield obj, 'main', case['case'], case['job']
        stratum = (obj['selection']['repository'].lower(), obj['selection']['workflow'])
        if stratum not in seen:
            seen.add(stratum)
            normal = next(c['job'] for c in obj['cases'] if c['case'] == 'normal')
            denied = copy.deepcopy(normal); denied['event']['type'] = 'rr022.undeclared'
            wrong = copy.deepcopy(normal); wrong['report_override'] = '{}'
            yield obj, 'diagnostic', 'undeclared_event', denied
            yield obj, 'diagnostic', 'wrong_content_control', wrong


def recheck_development():
    assert not RECHECK.exists()
    qualification = read(QUALIFY)
    assert len(qualification['rows']) == 36
    changed = [name for name, checksum in qualification['source_sha256'].items() if sha((ROOT / name).read_bytes()) != checksum]
    assert set(changed) <= {Path(__file__).relative_to(ROOT).as_posix(),
                           (HERE / 'check_formal_holdout.py').relative_to(ROOT).as_posix()}
    issue = next(s for s in read(RESULTS / 'archive-snapshots-20260930-v1.json')['snapshots'] if s['kind'] == 'issue')
    pr = read(RESULTS / 'pr-manifest-snapshot-20260930-v1.json')['snapshot']
    records = []
    for row in qualification['rows']:
        raw = Path((issue if row['kind'] == 'issue' else pr)['cache_path']).read_bytes()
        metrics = check_row(row, raw, expected_report(row['kind'], raw))
        records.append(dict(kind=row['kind'], case=row['case'], method=row['method'], checked=metrics))
    output = dict(status='passed', checked_utc=now(), rows=records, source_sha256=source_hashes(),
                  qualification_sha256=sha(QUALIFY.read_bytes()), changed_sources=changed,
                  correction='Admission OPA records have input/result; only call OPA records have error. '
                  'Rechecked existing 36 runs without repeating or changing method executions. '
                  'Formal driver schedules three isolated workers concurrently; no performance comparisons.')
    save(RECHECK, output)
    print(json.dumps(dict(status='passed', rows=len(records), changed_sources=changed)))


def run(output):
    assert not output.exists()
    freeze_record = read(FREEZE)
    assert freeze_record['method_execution_authorized'] and source_hashes() == freeze_record['source_sha256']
    assert sha(INPUTS.read_bytes()) == freeze_record['inputs_sha256']
    folder = Path(freeze_record['parser_folder'])
    for name, checksum in freeze_record['parser_generation']['generated_sha256'].items():
        assert sha((folder / 'spec_lang' / name).read_bytes()) == checksum
    inputs = read(INPUTS)
    result = dict(status='running', started_utc=now(), freeze_path=str(FREEZE), freeze_sha256=sha(FREEZE.read_bytes()),
                  rows=[], ungated=[], source_integrity_after=None)
    save(output, result)
    start = time.monotonic()
    # No oracle or expected report is loaded into a method worker.
    for obj, group, case, job in planned(inputs):
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {method: pool.submit(invoke, obj['target']['kind'], job, method, folder) for method in METHODS}
            completed = {method: future.result() for method, future in futures.items()}
        for method in METHODS:
            if time.monotonic() - start > 21600:
                result.update(status='budget_exhausted', finished_utc=now())
                save(output, result)
                return
            row = dict(object_id=obj['object_id'], kind=obj['target']['kind'], group=group, case=case, method=method,
                       job=job, **completed[method])
            result['rows'].append(row)
            save(output, result)
            print(json.dumps(dict(run=len(result['rows']), object_id=obj['object_id'], group=group, case=case,
                                  method=method, workflow=row['observed'].get('workflow'), error=row['observed'].get('error'))), flush=True)
    from run_archive_workflow_pilot import content_service, post_record
    for obj in inputs['objects']:
        for case in obj['cases']:
            if case['case'] == 'normal':
                continue
            job = case['job']
            directory = Path(tempfile.mkdtemp(prefix='holdout-ungated-', dir=ROOT / 'tmp/rr022-baselines'))
            control = dict(object_id=obj['object_id'], case=case['case'], error=None)
            try:
                with content_service(directory, job['catalog']) as (url, ledger, reports, ops):
                    tool, args = 'snapshot.read', dict(job['read_arguments'])
                    if case['case'] == 'other_write_object':
                        tool = 'report.write'
                        args.update(resource=job['write_resource'], report='{}')
                    control['response'] = post_record(url, dict(tool=tool, arguments=args))
                control.update(operations=ops, ledger=str(ledger), ledger_sha256=sha(ledger.read_bytes()))
            except Exception:
                control['error'] = traceback.format_exc()
            result['ungated'].append(control)
            save(output, result)
    result.update(status='executed_pending_check', finished_utc=now(),
                  source_integrity_after=source_hashes() == freeze_record['source_sha256'])
    save(output, result)
    print(json.dumps(dict(status=result['status'], rows=len(result['rows']), ungated=len(result['ungated']),
                          source_integrity_after=result['source_integrity_after'])))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['qualify', 'recheck', 'freeze', 'run'])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.stage == 'qualify':
        qualify()
    elif args.stage == 'recheck':
        recheck_development()
    elif args.stage == 'freeze':
        freeze()
    else:
        assert args.output
        run(args.output)
