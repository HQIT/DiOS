import copy
import unittest
from audit_workload_selection import inspect_input, summarize
from screen_workload_candidate_pool import rank_repositories


class InputAuditTests(unittest.TestCase):
    def issue(self):
        return {'id': '123', 'public': True, 'type': 'IssuesEvent', 'repo': {'name': 'Owner/Repo'},
                'created_at': '2026-09-21T00:00:00Z', 'payload': {'action': 'opened',
                'issue': {'number': 1, 'title': 'private-title', 'body': 'private-body', 'labels': [{'name': 'private-label'}]}}}

    def test_eligible_does_not_export_text(self):
        result = inspect_input(self.issue())
        self.assertEqual(result['status'], 'metadata_eligible_archived_content_present')
        self.assertNotIn('private-', str(result))

    def test_null_is_not_missing(self):
        e = self.issue(); e['payload']['issue']['body'] = None
        self.assertEqual(inspect_input(e)['body_state'], 'null')
        del e['payload']['issue']['body']
        self.assertEqual(inspect_input(e)['missing_fields'], ['body'])

    def test_blank_body_eligible(self):
        e = self.issue(); e['payload']['issue']['body'] = ' \n'
        self.assertEqual(inspect_input(e)['body_state'], 'blank')

    def test_labels_shape(self):
        e = self.issue(); e['payload']['issue']['labels'] = ['bug']
        self.assertEqual(inspect_input(e)['status'], 'invalid_business_field_types')

    def test_pr_is_not_issue(self):
        e = self.issue(); e['payload']['issue']['pull_request'] = {}
        self.assertEqual(inspect_input(e)['status'], 'invalid_identity_or_version')

    def test_non_opened(self):
        e = self.issue(); e['payload']['action'] = 'closed'
        self.assertEqual(inspect_input(e)['status'], 'outside_target_actions')

    def test_comment_is_not_opened(self):
        e = self.issue(); e['type'] = 'IssueCommentEvent'
        self.assertEqual(inspect_input(e)['status'], 'outside_target_event_types')

    def test_pr_requires_history(self):
        e = self.issue(); e['type'] = 'PullRequestEvent'
        e['payload'] = {'action': 'opened', 'number': 3, 'pull_request': {'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}}
        result = inspect_input(e)
        self.assertEqual(result['status'], 'metadata_eligible_history_unverified')
        self.assertFalse(result['historical_content_checked'])

    def test_object_dedup(self):
        row = {'inspection': inspect_input(self.issue())}
        self.assertEqual(summarize([row, copy.deepcopy(row)])['issue_objects'], 1)
        self.assertEqual(summarize([])['events'], 0)

    def test_pool_threshold_both_workflows(self):
        m = {'minimum_unique_objects_each_workflow': 3, 'excluded_development_repositories': ['Dev/Repo'], 'seed': 'fixed'}
        counts = {'a/b': {'issue_intake': 3, 'pr_change_manifest': 3},
                  'c/d': {'issue_intake': 99, 'pr_change_manifest': 2},
                  'dev/repo': {'issue_intake': 99, 'pr_change_manifest': 99}}
        self.assertEqual(rank_repositories(counts, m), ['a/b'])

    def test_pool_order_independent(self):
        m = {'minimum_unique_objects_each_workflow': 3, 'excluded_development_repositories': [], 'seed': 'fixed'}
        counts = {n: {'issue_intake': 3, 'pr_change_manifest': 3} for n in ('a/b', 'c/d', 'e/f')}
        self.assertEqual(rank_repositories(counts, m), rank_repositories(dict(reversed(list(counts.items()))), m))


if __name__ == '__main__':
    unittest.main()
