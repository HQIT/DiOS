import copy
import unittest
from holdout_selection import select_objects


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.protocol = dict(strata=[dict(repository='a/b', workflow='pr_change_manifest', maximum_objects=2)],
                             window=dict(start_utc='2026-09-22T00:00:00Z', end_utc_exclusive='2026-09-25T00:00:00Z'), selection=dict(seed='test'))
        self.excluded = dict(event_ids=[], object_groups=[], head_shas=[], excluded_dates=['2026-09-21'])
    def row(self, n, **extra):
        return dict(event_id=str(n), repository='a/b', workflow='pr_change_manifest', object_group=f'a/b:pr:{n}',
                    head_sha=str(n).zfill(40), archive_hour='2026-09-22-0', line=n,
                    inspection_status='metadata_eligible_history_unverified', created_at='2026-09-22T00:00:00Z', **extra)
    def test_empty(self):
        self.assertEqual(select_objects([], self.protocol, self.excluded)['independent_objects'], 0)
    def test_limit_order_invariant(self):
        rows = [self.row(n) for n in range(1, 10)]
        a = select_objects(rows, self.protocol, self.excluded)
        b = select_objects(list(reversed(rows)), self.protocol, self.excluded)
        self.assertEqual(a['selected'], b['selected']); self.assertEqual(len(a['selected']), 2)
    def test_exposures(self):
        for key, value in [('event_ids', '1'), ('object_groups', 'a/b:pr:1'), ('head_shas', '1'.zfill(40))]:
            excluded = copy.deepcopy(self.excluded); excluded[key] = [value]
            self.assertEqual(select_objects([self.row(1)], self.protocol, excluded)['independent_objects'], 0)
    def test_same_object_earliest_numeric_event(self):
        a, b = self.row(2), self.row(10); b['object_group'] = a['object_group']
        self.assertEqual(select_objects([b, a], self.protocol, self.excluded)['selected'][0]['event_id'], '2')
    def test_shared_head_lexical_group(self):
        a, b = self.row(2), self.row(10); b['head_sha'] = a['head_sha']
        out = select_objects([a, b], self.protocol, self.excluded)
        self.assertEqual(out['selected'][0]['event_id'], '10'); self.assertEqual(out['exclusion_counts']['shared_head'], 1)
    def test_end_exclusive(self):
        row = self.row(1); row['created_at'] = '2026-09-25T00:00:00Z'
        self.assertEqual(select_objects([row], self.protocol, self.excluded)['independent_objects'], 0)
    def test_wrong_stratum_or_invalid(self):
        for key, value in [('repository', 'c/d'), ('inspection_status', 'invalid_identity_or_version')]:
            row = self.row(1); row[key] = value
            self.assertEqual(select_objects([row], self.protocol, self.excluded)['independent_objects'], 0)
    def test_duplicates(self):
        row = self.row(1)
        self.assertEqual(select_objects([row, row], self.protocol, self.excluded)['exclusion_counts']['duplicate_event'], 1)


if __name__ == '__main__': unittest.main()
