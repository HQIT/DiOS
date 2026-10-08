"""Two already-shortlisted projects; one already-exposed PR each, no holdout."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request
import argparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    screen_path = HERE / 'results/workload-candidate-pool-screen-20260930-v1.json'
    screen = json.loads(screen_path.read_text(encoding='utf-8'))
    repos = ['arcadedata/arcadedb', 'elastic/kibana']
    assert all(repo in [r['repository'] for r in screen['shortlist']] for repo in repos)
    cache = Path(tempfile.mkdtemp(prefix='rr022-two-repo-feasibility-', dir=ROOT / 'tmp'))
    output = dict(status='running', projects=[], requests=[], source_sha256=sha(screen_path.read_bytes()),
                  script_sha256=sha(Path(__file__).read_bytes()),
                  selection='Purposive two-project proposal within existing shortlist, not random or hash-ranked final selection. Lowest archive hour/line exposed PR per project.',
                  limits=dict(requests=10, bytes_per_request=12000000, total_bytes=40000000), formal_runs=0)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://127.0.0.1:7078'}))
    def get(url):
        assert len(output['requests']) < 10
        record = dict(url=url)
        output['requests'].append(record)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'E2AG-readonly-research/1.0', 'Accept': 'application/vnd.github+json'})
            with opener.open(req, timeout=35) as response:
                raw = response.read(12000001)
                assert len(raw) <= 12000000
                assert sum(r.get('bytes', 0) for r in output['requests']) + len(raw) <= 40000000
                path = cache / (sha(url.encode()) + '.json')
                path.write_bytes(raw)
                record.update(status=response.status, bytes=len(raw), cache_path=str(path), sha256=sha(raw))
            return json.loads(raw)
        except Exception as exc:
            record['error'] = type(exc).__name__ + ': ' + str(exc)
            raise
        finally:
            args.output.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    for repo in repos:
        record = dict(repository=repo, status='checking', exploration_counts=screen['metadata_counts'][repo])
        output['projects'].append(record)
        try:
            candidates = [r for r in screen['eligible_records'] if r['repository'] == repo and r['workflow'] == 'pr_change_manifest']
            item = min(candidates, key=lambda r: (r['archive_hour'], r['line']))
            path = ROOT / 'tmp/rr022-gharchive-scout-v1' / (item['archive_hour'] + '.json.gz')
            with gzip.open(path, 'rb') as stream:
                raw = next(v for n, v in enumerate(stream, 1) if n == item['line'])
            assert sha(raw) == item['raw_line_sha256']
            event = json.loads(raw); pr = event['payload']['pull_request']
            record.update(exposed_pr=item, base=pr['base']['sha'], head=pr['head']['sha'])
            api = 'https://api.github.com/repos/' + repo
            metadata = get(api)
            record['metadata'] = {k: metadata.get(k) for k in ('full_name', 'fork', 'archived', 'disabled', 'created_at', 'language', 'description')}
            assert metadata['full_name'].lower() == repo and not metadata['archived'] and not metadata['disabled'] and not metadata['fork']
            comparison = get(api + '/compare/' + record['base'] + '...' + record['head'] + '?per_page=1')
            assert comparison['base_commit']['sha'] == record['base']
            before = comparison['merge_base_commit']
            head = get(api + '/git/commits/' + record['head'])
            assert head['sha'] == record['head']
            record['merge_base'] = before['sha']
            trees = []
            for name, tree_sha in [('before', before['commit']['tree']['sha']), ('after', head['tree']['sha'])]:
                tree = get(api + '/git/trees/' + tree_sha + '?recursive=1')
                trees.append(dict(side=name, sha=tree_sha, entries=len(tree['tree']), truncated=tree.get('truncated')))
                record['trees'] = trees
                assert tree['sha'] == tree_sha and tree.get('truncated') is False
            record['status'] = 'one_exposed_pr_feasible'
        except Exception as exc:
            record.update(status='not_verified', error=type(exc).__name__ + ': ' + str(exc))
        args.output.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(record), flush=True)
    output['status'] = 'checked'
    args.output.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
