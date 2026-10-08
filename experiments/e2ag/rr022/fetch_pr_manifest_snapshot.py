"""Bounded public read-only acquisition for the existing development PR only."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    ep = HERE / 'results/archive-development-examples-20260930-v3.json'
    assert sha(ep.read_bytes()) == 'ee17e89d69b2293d9d743590d0f1c661d6ec5ef2802727836f3b419bc233c64d'
    example = next(e for e in json.loads(ep.read_text(encoding='utf-8'))['examples'] if e['adaptation']['target']['kind'] == 'pr')
    target = example['adaptation']['target']
    assert target['repository'] == 'microsoft/vscode' and target['number'] == 337123
    cache = Path(tempfile.mkdtemp(prefix='pr-manifest-', dir=ROOT / 'tmp'))
    report = dict(status='running', target=target, source_event_id=example['source_record']['event_id'], requests=[],
                  examples_sha256=sha(ep.read_bytes()), script_sha256=sha(Path(__file__).read_bytes()),
                  budget=dict(max_requests=5, max_response_bytes=12000000, max_total_bytes=30000000),
                  boundary='Development object only. Exact archived base/head, no current-ref fallback, no code execution or remote writes.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://127.0.0.1:7078'}))
    used = 0
    def get(url):
        nonlocal used
        assert len(report['requests']) < 5
        record = dict(url=url)
        report['requests'].append(record)
        req = urllib.request.Request(url, headers={'User-Agent': 'E2AG-readonly-research/1.0', 'Accept': 'application/vnd.github+json'})
        try:
            with opener.open(req, timeout=40) as response:
                raw = response.read(12000001)
                assert len(raw) <= 12000000
                used += len(raw)
                assert used <= 30000000
                path = cache / (sha(url.encode()) + '.json')
                path.write_bytes(raw)
                record.update(status=response.status, response_sha256=sha(raw), bytes=len(raw), cache_path=str(path),
                              rate_remaining=response.headers.get('X-RateLimit-Remaining'))
            return json.loads(raw)
        except Exception as exc:
            record['error'] = type(exc).__name__ + ': ' + str(exc)
            raise
        finally:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps({k: v for k, v in record.items() if k != 'cache_path'}), flush=True)
    try:
        prefix = 'https://api.github.com/repos/' + target['repository']
        comparison = get(prefix + '/compare/' + target['base'] + '...' + target['head'] + '?per_page=1')
        assert comparison['base_commit']['sha'] == target['base']
        merge_base = comparison['merge_base_commit']['sha']
        commits = {name: get(prefix + '/git/commits/' + revision) for name, revision in [('before', merge_base), ('after', target['head'])]}
        assert commits['before']['sha'] == merge_base and commits['after']['sha'] == target['head']
        bundle = dict(repository=target['repository'], number=target['number'], base=target['base'], head=target['head'], merge_base=merge_base)
        for name in ('before', 'after'):
            tree_sha = commits[name]['tree']['sha']
            tree = get(prefix + '/git/trees/' + tree_sha + '?recursive=1')
            assert tree['sha'] == tree_sha and tree.get('truncated') is False
            bundle[name] = {'sha': tree_sha, 'truncated': False,
                            'tree': [{k: entry[k] for k in ('path', 'mode', 'type', 'sha')} for entry in tree['tree']]}
        raw = canonical(bundle)
        path = cache / (sha(raw) + '.snapshot')
        path.write_bytes(raw)
        report.update(status='complete', snapshot=dict(kind='pr', source_event_id=report['source_event_id'],
                      version=sha(raw), cache_path=str(path), content_sha256=sha(raw), bytes=len(raw),
                      line_count=1, representation='complete_merge_base_and_head_tree_entries', target=target),
                      merge_base=merge_base, tree_entries={name: len(bundle[name]['tree']) for name in ('before', 'after')})
    except Exception as exc:
        report.update(status='unavailable', error=type(exc).__name__ + ': ' + str(exc))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'bytes': used, 'error': report.get('error')}))
    if report['status'] != 'complete':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
