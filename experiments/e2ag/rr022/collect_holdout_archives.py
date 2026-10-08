"""Approved bounded archive collection. Select only after all hours validate."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import urllib.request
from audit_workload_selection import inspect_input
from scout_public_workload import digest, project_event
from holdout_selection import select_objects

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--freeze', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    freeze = json.loads(args.freeze.read_text(encoding='utf-8'))
    assert freeze['collection_authorized'] and not freeze['method_execution_authorized']
    for name, checksum in freeze['source_sha256'].items():
        assert digest(ROOT / name) == checksum, name
    protocol_path = HERE / freeze['protocol_path']
    assert digest(protocol_path) == freeze['protocol_sha256']
    protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
    exclusion_path = HERE / freeze['exclusion_path']
    assert digest(exclusion_path) == freeze['exclusion_sha256']
    exclusions = json.loads(exclusion_path.read_text(encoding='utf-8'))
    cache = ROOT / 'tmp/rr022-holdout-v0.4'
    cache.mkdir(parents=True, exist_ok=True)
    start = datetime.fromisoformat(protocol['window']['start_utc'].replace('Z', '+00:00'))
    hours = [(start + timedelta(hours=h)).strftime('%Y-%m-%d-') + str((start + timedelta(hours=h)).hour) for h in range(protocol['window']['hours'])]
    if args.output.exists():
        assert args.resume
        report = json.loads(args.output.read_text(encoding='utf-8'))
        assert report['freeze_sha256'] == digest(args.freeze) and report['status'] != 'sample_selected_pending_content_and_review'
    else:
        report = dict(status='collecting', freeze_sha256=digest(args.freeze), archives=[], candidate_rows=[], attempts=[],
                      started_at=datetime.now(timezone.utc).isoformat(), formal_method_runs=0)
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    save()
    names = {s['repository'].lower() for s in protocol['strata']}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://127.0.0.1:7078'}))
    total = sum(p.stat().st_size for p in cache.iterdir() if p.is_file())
    assert total <= protocol['limits']['archive_compressed_bytes']
    for hour in hours:
        target = cache / (hour + '.json.gz')
        old = next((a for a in report['archives'] if a['hour'] == hour), None)
        if old:
            assert target.exists() and digest(target) == old['sha256']
            continue
        record = dict(hour=hour, url='https://data.gharchive.org/' + hour + '.json.gz')
        try:
            if not target.exists():
                part = cache / (hour + '-' + str(len(report['attempts'])) + '.part')
                assert not part.exists()
                size = 0
                with opener.open(urllib.request.Request(record['url'], headers={'User-Agent': 'E2AG-readonly-research/1.0'}), timeout=45) as response, part.open('xb') as stream:
                    declared = response.headers.get('Content-Length')
                    record['content_length'] = declared
                    if declared:
                        assert int(declared) <= protocol['limits']['archive_bytes_per_hour']
                        assert total + int(declared) <= protocol['limits']['archive_compressed_bytes']
                    while block := response.read(1024 * 1024):
                        size += len(block)
                        assert size <= protocol['limits']['archive_bytes_per_hour'] and total + size <= protocol['limits']['archive_compressed_bytes'], 'Archive budget exceeded'
                        stream.write(block)
                    if declared: assert size == int(declared)
                total += size
                part.rename(target)
            types, rows, lines = Counter(), [], 0
            with gzip.open(target, 'rb') as stream:
                for lines, raw in enumerate(stream, 1):
                    event = json.loads(raw)
                    types[event.get('type', '(missing)')] += 1
                    if (event.get('repo') or {}).get('name', '').lower() not in names:
                        continue
                    if event.get('type') not in ('IssuesEvent', 'PullRequestEvent') or (event.get('payload') or {}).get('action') != 'opened':
                        continue
                    source = project_event(event, raw, hour, lines)
                    check = inspect_input(event)
                    rows.append(dict(event_id=str(source['event_id']), repository=source['repository'].lower(),
                                     created_at=source['created_at'], archive_hour=hour, line=lines, raw_line_sha256=source['raw_line_sha256'],
                                     object_group=check.get('object_group', source['object_group']), workflow=check.get('workflow'),
                                     inspection_status=check['status'], head_sha=check.get('head_sha'),
                                     base_sha=check.get('base_sha'), version=check.get('version')))
            record.update(status='complete', compressed_bytes=target.stat().st_size, sha256=digest(target), lines=lines,
                          event_types=dict(types), matching_opened=len(rows), cache_path=str(target))
            report['archives'].append(record)
            report['candidate_rows'].extend(rows)
            report['attempts'].append(dict(hour=hour, status='complete'))
            save()
            print(json.dumps({k: record[k] for k in ('hour', 'compressed_bytes', 'lines', 'matching_opened')}), flush=True)
        except Exception as exc:
            report['attempts'].append(dict(hour=hour, status='failed', error=type(exc).__name__ + ': ' + str(exc)))
            report['status'] = 'collection_incomplete_no_selection'
            save()
            print(json.dumps(report['attempts'][-1]), flush=True)
            raise SystemExit(1)
    assert len(report['archives']) == len(hours) == 72
    assert {a['hour'] for a in report['archives']} == set(hours)
    report['selection'] = select_objects(report['candidate_rows'], protocol, exclusions)
    report['status'] = 'sample_selected_pending_content_and_review'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({'status': report['status'], 'strata': report['selection']['strata'], 'objects': report['selection']['independent_objects']}), flush=True)


if __name__ == '__main__':
    main()
