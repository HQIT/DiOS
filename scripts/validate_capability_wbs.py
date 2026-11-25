"""Check WBS acceptance coverage, dependency DAG and evidence links."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    plan = json.loads((ROOT / 'docs/planning/project-plan.json').read_text())
    wbs = json.loads((ROOT / 'docs/planning/capability-wbs.json').read_text())
    tasks = {t['id']: t for t in plan['tasks']}
    rows = wbs['items']
    items = {i['id']: i for i in rows}
    assert len(items) == len(rows), 'duplicate work item'
    requirements = {r['id']: r for r in wbs['requirements']}
    assert len(requirements) == len(wbs['requirements']), 'duplicate requirement'
    tracked = {
        t['id'] for t in tasks.values()
        if t['product_scope'] != 'external-application'
        and ('closure_work_items' in t or t.get('status') != 'Done')
    }
    assert {r['parent'] for r in requirements.values()} == tracked, 'missing task coverage'
    for parent in tracked:
        task = tasks[parent]
        expected = {f'{parent}-A{n:02d}': text for n, text in enumerate(task['accept'], 1)}
        actual = {r['id']: r['text'] for r in requirements.values() if r['parent'] == parent}
        assert expected == actual, f'acceptance changed: {parent}'
        assert task['closure_work_items'] == [i['id'] for i in rows if i['parent'] == parent], parent
    for requirement in requirements.values():
        assert requirement['work_items'], f'uncovered: {requirement["id"]}'
        assert all(ref in items for ref in requirement['work_items']), requirement['id']
    allowed_states = {'todo', 'in_progress', 'verification_pending', 'review_pending', 'blocked', 'done'}
    for item in rows:
        assert item['parent'] in tracked, f'outside capability scope: {item["id"]}'
        assert item['kind'] in {'I', 'V', 'R'} and item['state'] in allowed_states
        for key in ('title', 'deliverable', 'done_when', 'verification', 'owner', 'basis', 'wave'):
            assert item[key], (item['id'], key)
        assert item['requirements'], f'no requirement: {item["id"]}'
        for req in item['requirements']:
            assert req in requirements and item['id'] in requirements[req]['work_items'], (item['id'], req)
        for dep in item['dependencies']:
            assert dep in items, f'unknown dependency: {dep}'
        for ref in item['existing_evidence']:
            assert (ROOT / ref).is_file(), f'missing evidence: {ref}'
        if item['state'] == 'done':
            assert item['completion_evidence'], f'done without evidence: {item["id"]}'
    visited, visiting = set(), set()

    def visit(item_id):
        assert item_id not in visiting, f'dependency cycle at {item_id}'
        if item_id in visited:
            return
        visiting.add(item_id)
        for dep in items[item_id]['dependencies']:
            visit(dep)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in items:
        visit(item_id)
    assert plan['execution_control'] == wbs['control'], 'control drift'
    assert wbs['control']['next_work_item'] in items
    active = [i for i in rows if i['kind'] == 'I' and i['state'] == 'in_progress']
    assert len(active) <= wbs['control']['implementation_wip_limit'], 'WIP exceeded'
    for group in wbs['shared_work']:
        assert group['owner'] in items and all(i in items for i in group['consumers'])
    print(json.dumps({'result': 'PASS', 'work_items': len(rows), 'requirements': len(requirements),
                      'by_kind': dict(Counter(i['kind'] for i in rows)),
                      'by_state': dict(Counter(i['state'] for i in rows)),
                      'dependency_graph': 'acyclic'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
