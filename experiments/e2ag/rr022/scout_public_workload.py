"""Read-only, bounded GH Archive feasibility inventory; no experiment decisions."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import urllib.request

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as src:
        for block in iter(lambda: src.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def scalar(value):
    return value if isinstance(value, (str, int, float, bool)) else None


def project_event(event, raw, hour, number):
    p = event.get('payload') or {}
    pr = p.get('pull_request') or {}
    issue = p.get('issue') or {}
    comment = p.get('comment') or {}
    repository = event['repo']['name']
    kind = event.get('type')
    object_number = p.get('number') if kind == 'PullRequestEvent' else issue.get('number')
    if object_number is None and pr:
        object_number = pr.get('number')
    group = None
    if object_number is not None:
        category = 'pr' if pr or issue.get('pull_request') else 'issue'
        group = f'{repository.lower()}:{category}:{object_number}'
    elif kind == 'PushEvent':
        group = f"{repository.lower()}:push:{p.get('push_id', event.get('id'))}"
    return {
        'event_id': scalar(event.get('id')), 'repository': repository,
        'event_type': scalar(kind), 'created_at': scalar(event.get('created_at')),
        'action': scalar(p.get('action')), 'object_number': scalar(object_number),
        'object_group': group, 'ref': scalar(p.get('ref')),
        'head': scalar(p.get('head')), 'before': scalar(p.get('before')),
        'pr_head_sha': scalar((pr.get('head') or {}).get('sha')),
        'pr_base_sha': scalar((pr.get('base') or {}).get('sha')),
        'payload_keys': sorted(p),
        'has_pr_body': isinstance(pr.get('body'), str),
        'has_issue_body': isinstance(issue.get('body'), str),
        'has_comment_body': isinstance(comment.get('body'), str),
        'commit_entries': len(p.get('commits') or []),
        'archive_hour': hour, 'line': number,
        'raw_line_sha256': hashlib.sha256(raw).hexdigest(),
    }


def summarize(rows):
    keys = ('event_type', 'action')
    return {
        'events': len(rows),
        **{key + '_counts': dict(sorted(Counter(str(r[key]) for r in rows).items())) for key in keys},
        'unique_object_groups': len({r['object_group'] for r in rows if r['object_group']}),
        'events_with_object_group': sum(r['object_group'] is not None for r in rows),
        'push_with_head_and_before': sum(bool(r['head'] and r['before']) for r in rows if r['event_type'] == 'PushEvent'),
        'pr_with_head_and_base': sum(bool(r['pr_head_sha'] and r['pr_base_sha']) for r in rows if r['event_type'] == 'PullRequestEvent'),
        'hours_present': sorted({r['archive_hour'] for r in rows}),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--proxy', default='http://127.0.0.1:7078')
    ap.add_argument('--manifest', type=Path, default=BASE / 'public-workload-scout-v1.json')
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite an existing report')
    manifest_path = args.manifest
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    cache = ROOT / 'tmp' / 'rr022-gharchive-scout-v1'
    cache.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': args.proxy, 'https': args.proxy}))
    headers = {'User-Agent': 'E2AG-research-readonly-feasibility/1.0'}
    names = {r['name'].lower() for r in manifest['repositories']}
    report = {'status': 'in_progress', 'manifest': manifest, 'manifest_sha256': digest(manifest_path),
              'script_sha256': digest(Path(__file__)), 'started_at': datetime.now(timezone.utc).isoformat(),
              'repository_metadata': [], 'archives': [], 'events': [], 'duplicate_event_ids': 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    for repo in manifest['repositories']:
        item = {'requested_name': repo['name'], 'api_url': 'https://api.github.com/repos/' + repo['name']}
        try:
            with opener.open(urllib.request.Request(item['api_url'], headers=headers), timeout=30) as res:
                data = json.load(res)
            for key in ('full_name', 'html_url', 'created_at', 'pushed_at', 'archived', 'disabled', 'default_branch', 'language', 'fork'):
                item[key] = data.get(key)
            item['license_spdx'] = (data.get('license') or {}).get('spdx_id')
            item['verified_at'] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            item['error'] = f'{type(exc).__name__}: {exc}'
        report['repository_metadata'].append(item)
        save()
    total = 0
    seen = set()
    for hour in manifest['hours']:
        url = 'https://data.gharchive.org/' + hour + '.json.gz'
        target = cache / (hour + '.json.gz')
        record = {'hour': hour, 'url': url}
        try:
            if not target.exists():
                partial = target.with_suffix('.part')
                size = 0
                with opener.open(urllib.request.Request(url, headers=headers), timeout=45) as res, partial.open('wb') as out:
                    record['http_headers'] = {k: res.headers.get(k) for k in ('Content-Length', 'ETag', 'Last-Modified')}
                    while block := res.read(1024 * 1024):
                        size += len(block)
                        if size > manifest['limits']['max_compressed_bytes_per_hour'] or total + size > manifest['limits']['max_total_compressed_bytes']:
                            raise ValueError('Configured download bound exceeded')
                        out.write(block)
                partial.replace(target)
            size = target.stat().st_size
            total += size
            if size > manifest['limits']['max_compressed_bytes_per_hour'] or total > manifest['limits']['max_total_compressed_bytes']:
                raise ValueError('Configured cache bound exceeded')
            record.update(compressed_bytes=size, sha256=digest(target))
            rows, lines, all_types = [], 0, Counter()
            with gzip.open(target, 'rb') as src:
                for lines, raw in enumerate(src, 1):
                    event = json.loads(raw)
                    all_types[event.get('type', '(absent)')] += 1
                    if (event.get('repo') or {}).get('name', '').lower() in names:
                        rows.append(project_event(event, raw, hour, lines))
            for row in rows:
                event_id = row['event_id']
                if event_id is None:
                    raise ValueError('Matching event without id')
                if event_id in seen:
                    report['duplicate_event_ids'] += 1
                else:
                    seen.add(event_id)
                    report['events'].append(row)
            record.update(status='complete', lines=lines, all_event_types=dict(sorted(all_types.items())), matching_rows=len(rows))
        except Exception as exc:
            record.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        report['archives'].append(record)
        save()
        print(json.dumps(record), flush=True)
    report['summaries'] = {repo['name']: summarize([r for r in report['events'] if r['repository'].lower() == repo['name'].lower()]) for repo in manifest['repositories']}
    report['status'] = 'complete' if all(a['status'] == 'complete' for a in report['archives']) else 'partial'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps(report['summaries'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
