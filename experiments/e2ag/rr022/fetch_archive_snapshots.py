"""Bounded read-only acquisition at archived commit SHA; never current ref fallback."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing overwrite')
    examples_path = BASE / 'results/archive-development-examples-20260930-v3.json'
    assert digest(examples_path.read_bytes()) == 'ee17e89d69b2293d9d743590d0f1c661d6ec5ef2802727836f3b419bc233c64d'
    examples = json.loads(examples_path.read_text(encoding='utf-8'))['examples']
    cache = ROOT / 'tmp/rr022-archive-snapshots-v1'
    cache.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://127.0.0.1:7078'}))
    report = {'protocol': 'archive-fixed-snapshot-acquisition-v1',
              'selection': 'README.md at exact archived push/PR head; archived issue JSON for issue. No fallback or current-state retrieval.',
              'examples_sha256': digest(examples_path.read_bytes()), 'script_sha256': digest(Path(__file__).read_bytes()),
              'snapshots': [], 'requests': []}

    def get(url):
        record = {'url': url}
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'E2AG-readonly-research/1.0', 'Accept': 'application/vnd.github+json'})
            with opener.open(req, timeout=30) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('Response byte limit exceeded')
                record.update(status=response.status, response_sha256=digest(raw), bytes=len(raw),
                              rate_remaining=response.headers.get('X-RateLimit-Remaining'))
            path = cache / (digest(url.encode()) + '.json')
            if path.exists():
                assert path.read_bytes() == raw, 'Existing response differs; use new cache version'
            else:
                path.write_bytes(raw)
            record['cache_path'] = str(path)
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            record.update(status=exc.code, error='HTTPError')
            raise
        except Exception as exc:
            record.update(error=type(exc).__name__ + ': ' + str(exc))
            raise
        finally:
            report['requests'].append(record)

    for example in examples:
        target = example['adaptation']['target']
        source = example['source_record']
        item = {'kind': target['kind'], 'source_event_id': source['event_id'], 'target': target}
        try:
            if target['kind'] in ('push', 'pr'):
                repo, head = target['repository'], target['head']
                commit_url = f'https://api.github.com/repos/{repo}/git/commits/{head}'
                commit = get(commit_url)
                assert commit['sha'] == head
                url = f'https://api.github.com/repos/{repo}/contents/README.md?ref={head}'
                content = get(url)
                assert content['type'] == 'file' and content['encoding'] == 'base64'
                raw = base64.b64decode(content['content'])
                assert len(raw) == content['size'] and len(raw) <= 1_000_000
                blob_sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                assert blob_sha == content['sha']
                item.update(version=head, content_path='README.md', retrieval_url=url,
                            commit_api_url=commit_url, tree_sha=commit['tree']['sha'], git_blob_sha=blob_sha,
                            representation='exact_commit_readme_bytes', text_utf8=True)
                raw.decode('utf-8')
            else:
                archive = ROOT / 'tmp/rr022-gharchive-scout-v1' / (source['archive_hour'] + '.json.gz')
                with gzip.open(archive, 'rb') as stream:
                    line = next(value for index, value in enumerate(stream, 1) if index == source['line'])
                assert digest(line) == source['raw_line_sha256']
                issue = json.loads(line)['payload']['issue']
                raw = json.dumps(issue, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
                assert digest(raw) == target['snapshot_sha256']
                item.update(version=target['snapshot_sha256'], content_path='archived-issue.json',
                            retrieval_url='https://data.gharchive.org/' + source['archive_hour'] + '.json.gz',
                            representation='canonical_archived_issue_json', text_utf8=True)
            path = cache / (digest(raw) + '.snapshot')
            if not path.exists():
                path.write_bytes(raw)
            assert digest(path.read_bytes()) == digest(raw)
            item.update(status='available', content_sha256=digest(raw), bytes=len(raw),
                        line_count=len(raw.splitlines()), cache_path=str(path))
        except Exception as exc:
            item.update(status='unavailable', error=type(exc).__name__ + ': ' + str(exc))
        report['snapshots'].append(item)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(item, ensure_ascii=False), flush=True)
    report['status'] = 'complete' if all(i['status'] == 'available' for i in report['snapshots']) else 'partial'
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
