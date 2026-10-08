"""Separate predicate-level boundary probes and real HTTP no-gate controls."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile

from bounded_baseline_adapters import BoundedOpaAdapter, agentspec_arguments_allowed
from run_archive_workflow_pilot import content_service, post_record
from run_paired_tool_pilot import ROOT, HERE, file_hash

sys.path.insert(0, str(ROOT / 'backend'))
from app.services.e2ag_arguments import check_arguments, valid_constraints


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('pilot', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    pilot = json.loads(args.pilot.read_text(encoding='utf-8'))
    normal = next(r for r in pilot['rows'] if r['case'] == 'normal' and r['method'] == 'opa')
    context = copy.deepcopy(normal['observed']['call_opa'][0]['input'])
    schema = {'version': 2, 'tools': {'report.write': {
        'resource': {'type': 'string', 'enum': ['isolated/report']},
        'report': {'type': 'string', 'minLength': 1, 'maxLength': 4}}}}
    assert valid_constraints(schema)
    context['grant']['argument_constraints'] = schema
    context['grant']['allowed_tools'] = ['report.write']
    adapter = BoundedOpaAdapter()
    adapter.context = context
    probes = [
        ('minimum', 'a', True), ('maximum', 'abcd', True), ('overflow', 'abcde', False),
        ('empty', '', False), ('chinese', '中文', True), ('emoji', '\U0001f642', True),
        ('combining', 'e\u0301', True), ('unicode_maximum', '中文\U0001f642e', True),
        ('unicode_overflow', '中文\U0001f642e\u0301', False), ('integer', 42, False),
        ('boolean', True, False), ('null', None, False), ('array', ['a'], False),
        ('object', {'a': 1}, False), ('surrogate', '\ud800', False),
    ]
    candidates = [(name, {'resource': 'isolated/report', 'report': value}, allow) for name, value, allow in probes]
    candidates += [('wrong_resource', {'resource': 'isolated/other', 'report': 'a'}, False),
                   ('missing_field', {'resource': 'isolated/report'}, False),
                   ('extra_field', {'resource': 'isolated/report', 'report': 'a', 'extra': 1}, False)]
    rows = []
    for name, candidate, expected in candidates:
        native, reason = check_arguments('report.write', candidate, schema)
        opa, opa_reason = adapter.authorize('tools/call', {'name': 'report.write', 'arguments': candidate}, ['report.write'], schema)
        external = agentspec_arguments_allowed(schema['tools']['report.write'], candidate)
        rows.append(dict(case=name, candidate=candidate, expected=expected, e2ag=native, opa=opa, agentspec_predicate=external,
                         reasons=[reason, opa_reason], passed=native == opa == external == expected))
    # Change the policy, not the candidate: verifies the length rule is effective.
    relaxed = copy.deepcopy(schema)
    relaxed['tools']['report.write']['report']['maxLength'] = 5
    adapter.context['grant']['argument_constraints'] = relaxed
    candidate = {'resource': 'isolated/report', 'report': 'abcde'}
    native, _ = check_arguments('report.write', candidate, relaxed)
    opa, _ = adapter.authorize('tools/call', {'name': 'report.write', 'arguments': candidate}, ['report.write'], relaxed)
    external = agentspec_arguments_allowed(relaxed['tools']['report.write'], candidate)
    rows.append(dict(case='relaxed_length_control', candidate=candidate, expected=True, e2ag=native, opa=opa,
                     agentspec_predicate=external, passed=native == opa == external == True))
    controls = []
    for case in ('other_read_object', 'wrong_read_version', 'other_write_object', 'oversize_report', 'wrong_report_type'):
        row = next(r for r in pilot['rows'] if r['case'] == case and r['method'] == 'e2ag')
        job = row['job']
        tool = 'snapshot.read' if case in ('other_read_object', 'wrong_read_version') else 'report.write'
        candidate = copy.deepcopy(job['read_arguments'])
        if tool == 'report.write':
            candidate.update(resource=job['write_resource'], report=job.get('report_override', '{"diagnostic":true}'))
        directory = Path(tempfile.mkdtemp(prefix='intake-no-gate-', dir=ROOT / 'tmp/rr022-baselines'))
        with content_service(directory, job['catalog']) as (url, ledger, reports, operations):
            reply = post_record(url, {'tool': tool, 'arguments': candidate})
        disk = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
        saved = [json.loads(line) for line in reports.read_text(encoding='utf-8').splitlines()]
        passed = (len(disk) == 1 and disk == operations and disk[0]['arguments'] == candidate
                  and 'result' in reply and saved == ([candidate] if tool == 'report.write' else []))
        controls.append(dict(case=case, tool=tool, candidate=candidate, passed=passed, ledger=str(ledger),
                             ledger_sha256=file_hash(ledger), reports=str(reports), reports_sha256=file_hash(reports)))
    paths = [Path(__file__), HERE / 'bounded_baseline_adapters.py', HERE / 'opa_bounded_strings_v2.rego',
             HERE / 'opa_call_context.rego', HERE / 'run_archive_workflow_pilot.py', ROOT / 'backend/app/services/e2ag_arguments.py']
    output = dict(status='passed' if all(r['passed'] for r in rows + controls) else 'failed',
                  pilot_sha256=file_hash(args.pilot), source_sha256={str(p.relative_to(ROOT)): file_hash(p) for p in paths},
                  boundary_probes=rows, opa_evaluations=adapter.evaluations, no_gate_controls=controls,
                  scope='19 predicate-level probes; AgentSpec custom predicate only here, official executor covered separately by pilot. Valid frozen schemas, not arbitrary schema equivalence. Five controls reuse the same HTTP tool without enforcement.')
    args.output.write_text(json.dumps(output, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(status=output['status'], boundary_probes=len(rows), no_gate_controls=len(controls))))
    assert output['status'] == 'passed'


if __name__ == '__main__':
    main()
