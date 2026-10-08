"""Deterministic change inventory from actual returned Git-tree snapshot bytes."""
import base64
from collections import Counter
import json
import re

REPORT_LIMIT = 65536  # Fixed development budget, independent of observed output.


def leaf_entries(tree):
    if tree.get('truncated') is not False or not isinstance(tree.get('tree'), list):
        raise ValueError('Incomplete tree')
    seen, leaves = set(), {}
    for entry in tree['tree']:
        path, mode, kind, sha = (entry[k] for k in ('path', 'mode', 'type', 'sha'))
        if type(path) is not str or not path or path in seen:
            raise ValueError('Invalid or duplicate path')
        seen.add(path)
        if type(sha) is not str or not re.fullmatch('[0-9a-f]{40}', sha):
            raise ValueError('Invalid object ID')
        if (kind, mode) not in {('tree', '040000'), ('blob', '100644'), ('blob', '100755'), ('blob', '120000'), ('commit', '160000')}:
            raise ValueError('Unsupported Git entry')
        if kind != 'tree':
            leaves[path] = dict(type=kind, mode=mode, sha=sha)
    return leaves


def manifest_report(raw):
    bundle = json.loads(raw)
    before, after = leaf_entries(bundle['before']), leaf_entries(bundle['after'])
    changes, counts = [], Counter(added=0, deleted=0, modified=0, submodule_paths=0)
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        action = 'added' if old is None else 'deleted' if new is None else 'modified'
        changes.append(dict(path=path, change=action, before=old, after=new))
        counts[action] += 1
        counts['submodule_paths'] += int(any(e and e['type'] == 'commit' for e in (old, new)))
    return dict(repository=bundle['repository'], number=bundle['number'], base=bundle['base'], head=bundle['head'],
                merge_base=bundle['merge_base'], counts=dict(counts), changes=changes)


def report_arguments(job, read_result):
    report = json.dumps(manifest_report(base64.b64decode(read_result['content_base64'])), ensure_ascii=False, sort_keys=True)
    if 'report_override' in job:
        report = job['report_override']
    return {**job['read_arguments'], 'resource': job['write_resource'], 'report': report}
