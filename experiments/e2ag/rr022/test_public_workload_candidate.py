"""Synthetic tests only; no formal sample selection or network."""
import copy
import json
from pathlib import Path
import unittest
from check_public_workload_candidate import validate, exclusion_sets


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.m = json.loads((Path(__file__).with_name('public-workload-freeze-candidate-v0.2.json')).read_text(encoding='utf-8'))

    def test_exact_hours(self):
        hours = validate(self.m)
        self.assertEqual(len(hours), 72)
        self.assertEqual((hours[0], hours[-1]), ('2026-09-22-0', '2026-09-24-23'))

    def test_no_approval_by_preflight(self):
        self.m['formal_execution_enabled'] = True
        with self.assertRaises(ValueError): validate(self.m)

    def test_no_implicit_approval(self):
        self.m['author_approval'] = 'assumed'
        with self.assertRaises(ValueError): validate(self.m)

    def test_exploration_date_rejected(self):
        self.m['exploration_dates'].append('2026-09-23')
        with self.assertRaises(ValueError): validate(self.m)

    def test_repository_casefold(self):
        self.m['holdout_repositories'][0] = 'Microsoft/VSCode'
        with self.assertRaises(ValueError): validate(self.m)

    def test_duplicate_repository(self):
        self.m['holdout_repositories'][0] = 'FASTAPI/FASTAPI'
        with self.assertRaises(ValueError): validate(self.m)

    def test_run_inflation(self):
        self.m['comparison']['max_paired_runs'] = 1201
        with self.assertRaises(ValueError): validate(self.m)

    def test_host_mislabel(self):
        self.m['comparison']['shared_agentspec_admission'] = False
        with self.assertRaises(ValueError): validate(self.m)

    def test_prefilled_review(self):
        self.m['review']['answers_prefilled'] = True
        with self.assertRaises(ValueError): validate(self.m)

    def test_incomplete_gate(self):
        self.m['required_before_sampling'].remove('development_business_workflows_passed')
        with self.assertRaises(ValueError): validate(self.m)

    def test_union_and_no_common_ancestor_exclusion(self):
        r = {'status': 'complete', 'events': [{'event_id': '1', 'object_group': 'O/R:pr:1', 'head': 'abc', 'pr_head_sha': 'def', 'before': 'base'}]}
        self.assertEqual(exclusion_sets([r, copy.deepcopy(r)]), {'event_ids': ['1'], 'object_groups': ['o/r:pr:1'], 'head_shas': ['abc', 'def']})

    def test_partial_inventory(self):
        with self.assertRaises(ValueError): exclusion_sets([{'status': 'partial', 'events': []}])


if __name__ == '__main__':
    unittest.main()
