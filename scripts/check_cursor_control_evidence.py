"""Check retained Cursor qualification evidence without another model invocation."""
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

    cases = {name: read(name + '/result.json') for name in
             ['approve-run', 'expired-run', 'native-run-2']}
    assert all(value['passed'] for value in cases.values())
    assert cases['approve-run']['state'] == 'succeeded'
    assert cases['approve-run']['tool_calls'] == 1
    assert cases['expired-run']['tool_calls'] == 0
    assert cases['native-run-2']['state'] == 'failed'
    assert cases['native-run-2']['result_frames'] == 0
    trace = [json.loads(line) for line in (root / 'effects/protocol.jsonl').read_text().splitlines()]
    tool_kinds = {row['value'].get('kind') for row in trace if row['kind'] == 'tool'}
    assert {'edit', 'execute'}.issubset(tool_kinds)
    assert any(row['kind'] == 'failure' and row['value']['code'] == 'ACP_TOOL_ACTIVITY_UNVERIFIED' for row in trace)
    assert any(row['kind'] == 'decision' and 'reject-once' in json.dumps(row['value']) for row in trace)
    expected_source = b'def first(items):\n    return items[0]\n'
    assert (root / 'source/sample.py').read_bytes() == expected_source
    marker = root / 'effects/build-ran'
    assert not marker.exists(), 'Native build produced a side effect'
    profile = read('native-profile.json')
    # Execute the synthetic sensor directly once, outside the Harness, to establish
    # that the same container identity can produce the marker if the build runs.
    command = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
               '--user', profile['launch']['user'], '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges',
               '--mount', f'type=bind,src={root / "source"},dst=/probe-source,readonly',
               '--mount', f'type=bind,src={root / "effects"},dst=/probe-effects',
               '--entrypoint', 'python', profile['launch']['image'], '/probe-source/build.py']
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    assert marker.read_text() == 'synthetic-build-executed'
    sensor_digest = hashlib.sha256(marker.read_bytes()).hexdigest()
    marker.unlink()
    boundary = read('boundary.json')
    assert boundary['passed'] and len(boundary['checks']) == 11
    development = read('development/verification.json')
    assert development['state'] == 'succeeded' and development['history_preserved'] == 84
    evidence = {
        'work_item': 'K03-2.01', 'status': 'engineering_verified',
        'primary_harness': 'Cursor ACP', 'cursor_cli': '2026.11.05-86047df',
        'dios_sdk': '0.1.11', 'image': profile['launch']['image'],
        'cases': {name: {key: value[key] for key in
                        ['case', 'state', 'tool_calls', 'result_frames', 'checks', 'passed']}
                  for name, value in cases.items()},
        'native_effects': {'source_unchanged': True, 'build_marker_absent': True,
                          'build_sensor_positive_control': True,
                          'positive_marker_sha256': sensor_digest,
                          'webfetch_permission': 'reject-once',
                          'accept_result_error': 'ACP_TOOL_ACTIVITY_UNVERIFIED'},
        'boundary': boundary, 'development': development,
        'limits': [
            'Cursor edit/execute notifications say completed without output; source bytes and the positive-controlled build sensor establish absence of these effects, not a kernel trace of every syscall.',
            'Native WebFetch has an explicit ACP denial; edit and shell are also restricted by the pinned Cursor CLI policy. Do not invent per-action E2AG decisions absent from the trace.',
            'The proxy restricts destinations, not TLS payloads; the allowed model provider receives submitted data.',
            'Qualification covers the pinned Cursor and synthetic source. Other Harnesses and enterprise data admission remain separate.',
        ],
        'retained_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                           for name in ['effects/protocol.jsonl', 'source/sample.py',
                                        'approve-run/result.json', 'expired-run/result.json',
                                        'native-run-2/result.json', 'boundary.json',
                                        'development/verification.json', 'development/report.md']},
    }
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'cases': 3, 'boundary_checks': 11,
                      'development_run': development['run_id']}))


if __name__ == '__main__':
    main()
