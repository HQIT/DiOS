"""Verify retained DSH real-tool controls without another model invocation."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.directory.resolve()
    def read(name):
        return json.loads((root / name).read_text())
    names = ['final-approve', 'final-approval-expired', 'final-ancestor-revoke',
             'final-stale-epoch', 'native-enforced', 'release-approve']
    cases = {name: read(name + '/result.json') for name in names}
    assert all(case['passed'] for case in cases.values())
    for name in names:
        case = cases[name]
        positive = name in {'final-approve', 'release-approve'}
        assert case['state'] == ('succeeded' if positive else 'failed')
        assert case['tool_calls'] == (1 if positive or name == 'native-enforced' else 0)
        assert case['result_frames'] == (1 if positive else 0)
        assert case['attempt_id'] and case['session_id'] and case['process_id']
    trace = [json.loads(line) for line in (root / 'enforced-effects/protocol.jsonl').read_text().splitlines()]
    calls = {}
    for row in trace:
        if row['kind'] == 'tool':
            value = row['value']
            calls.setdefault(value['toolCallId'], {}).update(value)
    rejected = {}
    for name in ['read', 'edit', 'bash', 'web_fetch']:
        rows = [call for call in calls.values() if call.get('title') == name]
        assert rows and all(call.get('status') == 'failed' and
                           'DIOS_NATIVE_TOOL_DENIED' in json.dumps(call.get('content')) for call in rows), name
        rejected[name] = [call['toolCallId'] for call in rows]
    assert any(row['kind'] == 'failure' and row['value']['code'] == 'ACP_NATIVE_OPERATION_DENIED' for row in trace)
    assert (root / 'source/sample.py').read_bytes() == b'def first(items):\n    return items[0]\n'
    marker = root / 'enforced-effects/build-ran'
    assert not marker.exists()
    profile = read('native-profile.json')
    command = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--user', '65534:65534',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--mount', f'type=bind,src={root}/source,dst=/probe-source,readonly',
               '--mount', f'type=bind,src={root}/enforced-effects,dst=/probe-effects',
               '--entrypoint', 'python', profile['launch']['image'], '/probe-source/build.py']
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    assert marker.read_text() == 'synthetic-build-executed'
    marker.unlink()
    boundary, startup = read('boundary.json'), read('startup.json')
    assert boundary['passed'] and len(boundary['checks']) == 11
    assert startup['passed'] and startup['cases']['present']['initialized']
    assert not startup['cases']['missing']['initialized']
    retained = ['enforced-effects/protocol.jsonl', 'source/sample.py', 'boundary.json', 'startup.json']
    retained += [name + '/result.json' for name in names]
    keys = ['case', 'run_id', 'attempt_id', 'session_id', 'process_id', 'image', 'state',
            'attempt_error', 'tool_calls', 'result_frames', 'checks', 'exit_receipt_sha256', 'passed']
    evidence = {
        'work_item': 'K01-5.01', 'status': 'engineering_verified',
        'dsh': '0.2.2-rc.5', 'dios_sdk': '0.1.19',
        'cases': {name: {key: case[key] for key in keys} for name, case in cases.items()},
        'native_guard': {'denied_before_body': rejected, 'source_unchanged': True,
                         'build_marker_absent': True, 'build_sensor_positive_control': True},
        'boundary': boundary, 'startup': startup,
        'cursor_reused': ['docs/design/evidence/cursor-native-controls-20261126.json',
                          'docs/design/evidence/harness-governance-20261125.json',
                          'docs/design/evidence/cursor-recovery-upgrade-20261126.json'],
        'deployment': {'image': cases['release-approve']['image'], 'scope': 'isolated development qualification',
                       'case': 'release-approve', 'main_preview_changed': False},
        'regression': {'worker_and_secret_partition': {'passed': 15, 'skipped': 1},
                       'native_docker_host_proxy_injection': {'passed': 1}},
        'limits': [
            'Only the pinned Cursor and DSH compositions are qualified. Other executors, plugins and versions require qualification.',
            'DSH read-only mode ran the synthetic build body before denying its write; native controls require the monotonic guard. The pre-guard trace remains retained and is not passing execution-denial evidence.',
            'The native denial trace precedes only the added ACP startup dependency. The denial predicate is unchanged; startup presence/absence and the final immutable release positive path were separately verified.',
            'The proxy restricts destination hosts and ports, not TLS payloads. The allowed model provider receives submitted content.',
            'High-impact independent review and physical cross-host qualification remain separate WBS items.',
        ],
        'retained_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in retained},
    }
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'real_cases': len(cases), 'native_controls': len(rejected), 'boundary_checks': 11}))


if __name__ == '__main__':
    main()
