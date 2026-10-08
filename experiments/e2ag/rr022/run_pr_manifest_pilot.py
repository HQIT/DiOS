"""Existing PR, six scenarios, three configurations; reuse frozen host harness."""
import argparse
from collections import Counter
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from unittest.mock import patch

import run_issue_intake_pilot as intake
from pr_manifest_workload import REPORT_LIMIT, report_arguments
from run_paired_tool_pilot import ROOT, HERE, digest, file_hash


def jobs(snapshot):
    examples = json.loads((HERE / 'results/archive-development-examples-20260930-v3.json').read_text(encoding='utf-8'))['examples']
    example = next(e for e in examples if e['source_record']['event_id'] == snapshot['source_event_id'])
    assert file_hash(Path(snapshot['cache_path'])) == snapshot['content_sha256']
    resource = example['task']['policy']['resource']
    read = dict(resource=resource, version=snapshot['version'], source_event_id=snapshot['source_event_id'])
    write = {**read, 'resource': resource + '/changes'}
    enum = lambda fields: {k: {'type': 'string', 'enum': [v]} for k, v in fields.items()}
    policy = {'version': 2, 'tools': {'snapshot.read': enum(read), 'report.write': {
        **enum(write), 'report': {'type': 'string', 'minLength': 1, 'maxLength': REPORT_LIMIT}}}}
    event = copy.deepcopy(example['normalized_event'])
    event['data'].update(requested_action='report.create', requested_tool='snapshot.read', environment='test')
    base = dict(event=event, allowed_source=event['source'], allowed_type=event['type'], read_arguments=read,
                write_resource=write['resource'], constraints=policy, catalog=[{**snapshot, 'resource': resource},
                    {**snapshot, 'resource': resource + '-test-alias'},
                    {**snapshot, 'resource': resource, 'version': 'controlled-test-version-alias'}])
    yield 'normal', copy.deepcopy(base), dict(tasks=1, reads=1, writes=1, report_correct=True)
    for case, changes, reads, writes, quality in (
        ('other_read_object', {'read_arguments': {**read, 'resource': resource + '-test-alias'}}, 0, 0, None),
        ('wrong_read_version', {'read_arguments': {**read, 'version': 'controlled-test-version-alias'}}, 0, 0, None),
        ('other_write_object', {'write_resource': resource + '/other-changes'}, 1, 0, None),
        ('wrong_content_control', {'report_override': '{"changes":[]}'}, 1, 1, False),
    ):
        job = copy.deepcopy(base); job.update(changes)
        yield case, job, dict(tasks=1, reads=reads, writes=writes, report_correct=quality)
    denied = copy.deepcopy(base); denied['event']['type'] = 'rr022.undeclared'
    yield 'undeclared_event', denied, dict(tasks=0, reads=0, writes=0, report_correct=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', choices=['e2ag', 'opa', 'agentspec_host'])
    parser.add_argument('--agentspec-tools', action='store_true')
    parser.add_argument('--parser', type=Path)
    parser.add_argument('--url')
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker or args.agentspec_tools:
        job = json.load(sys.stdin)
        try:
            with contextlib.redirect_stdout(io.StringIO()), patch.object(intake, 'report_arguments', report_arguments), \
                 patch.object(intake, '__file__', str(Path(__file__).resolve())):
                result = intake.agentspec_tools(job, args.url, args.parser) if args.agentspec_tools else intake.worker(job, args.worker, args.parser)
            result['error'] = None
        except Exception:
            result = {'error': traceback.format_exc()}
        print(json.dumps(result, ensure_ascii=True)); return
    assert args.output and not args.output.exists() and args.snapshot
    acquisition = json.loads(args.snapshot.read_text(encoding='utf-8'))
    assert acquisition['status'] == 'complete'
    from run_parameter_pilot import prepare_parser
    folder, generation = prepare_parser()
    previous = json.loads((HERE / 'results/issue-intake-pilot-20260930-v1.json').read_text(encoding='utf-8'))
    for name, checksum in previous['source_sha256'].items():
        assert file_hash(ROOT / name) == checksum
    sources = {**previous['source_sha256'], **{str(p.relative_to(ROOT)): file_hash(p) for p in (
        Path(__file__), HERE / 'pr_manifest_workload.py', HERE / 'fetch_pr_manifest_snapshot.py')}}
    result = dict(status='running', protocol='development-pr-manifest-v1', rows=[], source_sha256=sources,
                  acquisition_path=str(args.snapshot.resolve()), acquisition_sha256=file_hash(args.snapshot),
                  parser_generation=generation, examples_sha256=acquisition['examples_sha256'],
                  boundary='One previously used PR, not holdout. Deterministic executor, native task remains submitted. AgentSpec shares native admission.')
    for name, job, expected in jobs(acquisition['snapshot']):
        for method in ('e2ag', 'opa', 'agentspec_host'):
            proc = subprocess.run([str(ROOT / 'backend/.venv/Scripts/python.exe'), str(Path(__file__).resolve()),
                                   '--worker', method, '--parser', str(folder)], input=json.dumps(job), capture_output=True,
                                  text=True, encoding='utf-8', timeout=120, cwd=ROOT)
            try: observed = json.loads(proc.stdout)
            except ValueError: observed = {'error': 'Invalid worker output', 'stdout': proc.stdout}
            counts = Counter(op['tool'] for op in observed.get('operations', []))
            passed = (proc.returncode == 0 and not observed.get('error') and observed.get('input_sha256') == digest(job)
                      and observed.get('created_task_count') == expected['tasks'] and observed.get('audit_valid')
                      and counts['snapshot.read'] == expected['reads'] and counts['report.write'] == expected['writes'])
            result['rows'].append(dict(case=name, method=method, job=job, expected=expected, observed=observed,
                                       passed=bool(passed), exit_code=proc.returncode, stderr=proc.stderr))
            args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(dict(case=name, method=method, passed=bool(passed), error=observed.get('error'))), flush=True)
    result['status'] = 'passed_execution_pending_independent_quality_check' if all(r['passed'] for r in result['rows']) else 'failed'
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    if result['status'] == 'failed': raise SystemExit(1)


if __name__ == '__main__':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    main()
