"""Selected objects -> immutable source snapshots, jobs and separate oracle.

No governance evaluation. Unavailable selected objects stay in the denominator.
"""
import argparse
import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import urllib.request
from archive_development_adapter import adapt
from scout_public_workload import digest
from check_pr_manifest_pilot import verify_tree, independent_inventory
from run_issue_intake_pilot import intake_report
from pr_manifest_workload import manifest_report

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def make_jobs(event, adapted, snapshot, policy_limits):
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.connectors.builtin.git_webhook.normalizers import GitHubNormalizer
    normalized = GitHubNormalizer().normalize({'x-github-event': adapted['normalizer_event_name']}, adapted['projected_payload'])
    normalized.update(id='gharchive:' + event['id'], time=event['created_at'])
    normalized['data'].update(requested_action='report.create', requested_tool='snapshot.read', environment='test')
    target = adapted['target']
    resource = f"rr022-holdout/{target['repository'].lower()}/{target['kind']}/{target['number']}"
    read = dict(resource=resource, version=snapshot['version'], source_event_id=event['id'])
    write = {**read, 'resource': resource + ('/intake' if target['kind'] == 'issue' else '/changes')}
    enum = lambda fields: {k: {'type': 'string', 'enum': [v]} for k, v in fields.items()}
    policy = dict(version=2, tools={'snapshot.read': enum(read), 'report.write': {
        **enum(write), 'report': dict(type='string', minLength=1, maxLength=policy_limits['report_max_codepoints'])}})
    base = dict(event=normalized, allowed_source=normalized['source'], allowed_type=normalized['type'], constraints=policy,
                read_arguments=read, write_resource=write['resource'], catalog=[{**snapshot, 'resource': resource},
                    {**snapshot, 'resource': resource + '-test-alias'},
                    {**snapshot, 'resource': resource, 'version': 'controlled-test-version-alias'}])
    jobs = [dict(case='normal', job=copy.deepcopy(base))]
    for case, changes in [('other_read_object', {'read_arguments': {**read, 'resource': resource + '-test-alias'}}),
                          ('other_write_object', {'write_resource': resource + '/other-report'}),
                          ('wrong_read_version', {'read_arguments': {**read, 'version': 'controlled-test-version-alias'}})]:
        job = copy.deepcopy(base); job.update(changes)
        jobs.append(dict(case=case, job=job))
    return jobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--collection', type=Path, required=True)
    parser.add_argument('--freeze', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--oracle', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists() and not args.oracle.exists()
    freeze = json.loads(args.freeze.read_text(encoding='utf-8'))
    assert freeze['collection_authorized'] and not freeze['method_execution_authorized']
    assert all(digest(ROOT / name) == value for name, value in freeze['source_sha256'].items())
    protocol = json.loads((HERE / freeze['protocol_path']).read_text(encoding='utf-8'))
    collection = json.loads(args.collection.read_text(encoding='utf-8'))
    assert collection['status'] == 'sample_selected_pending_content_and_review' and collection['freeze_sha256'] == digest(args.freeze)
    cache = Path(tempfile.mkdtemp(prefix='rr022-holdout-content-', dir=ROOT / 'tmp'))
    output = dict(status='preparing', collection_sha256=digest(args.collection), freeze_sha256=digest(args.freeze),
                  script_sha256=digest(Path(__file__)), objects=[], requests=[], formal_method_runs=0)
    oracle = dict(status='preparing_not_for_reviewers', objects=[])
    memo, stored = {}, 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://127.0.0.1:7078'}))
    def save():
        args.output.write_text(json.dumps(output, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        args.oracle.write_text(json.dumps(oracle, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    def store(raw, suffix):
        nonlocal stored
        path = cache / (checksum(raw) + suffix)
        if not path.exists():
            assert stored + len(raw) <= protocol['limits']['snapshot_cache_bytes'], 'Snapshot budget exceeded'
            path.write_bytes(raw); stored += len(raw)
        return path
    def get(url):
        if url in memo: return memo[url]
        assert len(output['requests']) < protocol['limits']['historical_api_attempts'], 'API attempt budget exceeded'
        record = dict(url=url)
        output['requests'].append(record)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'E2AG-readonly-research/1.0', 'Accept': 'application/vnd.github+json'})
            with opener.open(req, timeout=40) as response:
                raw = response.read(protocol['limits']['historical_response_bytes'] + 1)
                record.update(status=response.status, bytes_read=len(raw))
                assert len(raw) <= protocol['limits']['historical_response_bytes'], 'Historical response exceeds frozen byte limit'
            path = store(raw, '.json')
            record.update(cache_path=str(path), sha256=checksum(raw))
            memo[url] = json.loads(raw)
            return memo[url]
        except Exception as exc:
            record['error'] = type(exc).__name__ + ': ' + str(exc)
            raise
        finally: save()
    # Read only selected archive lines; preserve source hashes without exporting actor.
    raw_events = {}
    for hour in sorted({r['archive_hour'] for r in collection['selection']['selected']}):
        archive = next(a for a in collection['archives'] if a['hour'] == hour)
        assert digest(Path(archive['cache_path'])) == archive['sha256']
        wanted = {r['line']: r for r in collection['selection']['selected'] if r['archive_hour'] == hour}
        with gzip.open(archive['cache_path'], 'rb') as stream:
            for n, raw in enumerate(stream, 1):
                if n in wanted:
                    assert checksum(raw) == wanted[n]['raw_line_sha256']
                    raw_events[wanted[n]['event_id']] = json.loads(raw)
    for index, selected in enumerate(collection['selection']['selected'], 1):
        record = dict(object_id=f'H{index:03d}', selection=selected, status='preparing')
        output['objects'].append(record)
        try:
            event = raw_events[selected['event_id']]
            adapted = adapt(event)
            target = adapted['target']; record['target'] = target
            if target['kind'] == 'issue':
                issue = event['payload']['issue']
                raw = canonical(issue)
                wanted = dict(title_present=bool(issue['title'].strip()), title_characters=sum(1 for _ in issue['title']),
                              body_characters=sum(1 for _ in (issue['body'] or '')), labels=sorted(set(e['name'] for e in issue['labels'])),
                              queue='awaiting_triage' if issue['title'].strip() and (issue['body'] or '').strip() else 'needs_information')
                generated = intake_report(raw)
                evidence = dict(title=issue['title'], body=issue['body'], labels=[e['name'] for e in issue['labels']])
            else:
                api = 'https://api.github.com/repos/' + target['repository']
                comparison = get(api + '/compare/' + target['base'] + '...' + target['head'] + '?per_page=1')
                assert comparison['base_commit']['sha'] == target['base']
                merge = comparison['merge_base_commit']
                head = get(api + '/git/commits/' + target['head'])
                assert head['sha'] == target['head']
                bundle = dict(repository=target['repository'], number=target['number'], base=target['base'], head=target['head'], merge_base=merge['sha'])
                for name, tree_sha in [('before', merge['commit']['tree']['sha']), ('after', head['tree']['sha'])]:
                    tree = get(api + '/git/trees/' + tree_sha + '?recursive=1')
                    assert tree['sha'] == tree_sha and tree.get('truncated') is False, 'Truncated or mismatched tree'
                    verify_tree(tree)
                    bundle[name] = dict(sha=tree_sha, truncated=False, tree=[{k: e[k] for k in ('path', 'mode', 'type', 'sha')} for e in tree['tree']])
                raw = canonical(bundle)
                wanted, generated = independent_inventory(bundle), manifest_report(raw)
                evidence = dict(base=bundle['base'], head=bundle['head'], merge_base=bundle['merge_base'],
                                tree_sha_before=bundle['before']['sha'], tree_sha_after=bundle['after']['sha'],
                                source_tree_entries={name: bundle[name]['tree'] for name in ('before', 'after')})
            assert wanted == generated, 'Business transformation disagrees with independent oracle; no tuning permitted'
            snapshot_path = store(raw, '.snapshot')
            evidence_path = store(canonical(evidence), '.evidence.json')
            snapshot = dict(kind=target['kind'], version=checksum(raw), content_sha256=checksum(raw), source_event_id=event['id'],
                            cache_path=str(snapshot_path), bytes=len(raw), line_count=len(raw.splitlines()))
            report = json.dumps(generated, ensure_ascii=False, sort_keys=True)
            record.update(snapshot=snapshot, source_evidence_path=str(evidence_path), report_codepoints=len(report),
                          cases=make_jobs(event, adapted, snapshot, protocol['workflows'][selected['workflow']]),
                          normal_candidate_report=report, status='ready_for_independent_review')
            if len(report) > protocol['workflows'][selected['workflow']]['report_max_codepoints']:
                record['status'] = 'task_budget_failure_retained'
            oracle['objects'].append(dict(object_id=record['object_id'], expected_report=wanted,
                cases=[dict(case=name, task_should_be_created='是', read_should_execute='否' if name in ('other_read_object', 'wrong_read_version') else '是',
                            write_should_execute='是' if name == 'normal' else '否' if name == 'other_write_object' else '前序应终止')
                       for name in protocol['comparison']['main_cases']]))
        except Exception as exc:
            record.update(status='source_or_preparation_failure_retained', error=type(exc).__name__ + ': ' + str(exc))
        save()
        print(json.dumps(dict(object_id=record['object_id'], status=record['status'], error=record.get('error'))), flush=True)
    output['status'] = 'inputs_prepared_methods_wait_for_review'
    output['selected_count'] = len(output['objects'])
    output['ready_count'] = sum(o['status'] == 'ready_for_independent_review' for o in output['objects'])
    oracle['status'] = 'oracle_frozen_not_for_reviewers'
    save()
    print(json.dumps(dict(status=output['status'], selected=output['selected_count'], ready=output['ready_count'])))


if __name__ == '__main__': main()
