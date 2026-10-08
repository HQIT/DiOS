"""Projection/grouping tests; no network access."""
import unittest
from scout_public_workload import project_event, summarize


class ProjectionTests(unittest.TestCase):
    def event(self, kind, payload):
        return project_event({'id': '1', 'type': kind, 'repo': {'name': 'Owner/Repo'},
                              'actor': {'login': 'not-exported'}, 'payload': payload}, b'fixture\n', 'test', 1)

    def test_issue_group(self):
        row = self.event('IssuesEvent', {'issue': {'number': 42, 'body': 'not-exported'}})
        self.assertEqual(row['object_group'], 'owner/repo:issue:42')
        self.assertTrue(row['has_issue_body'])
        self.assertNotIn('not-exported', str(row))

    def test_pr_and_issue_comment_group_together(self):
        pr = self.event('PullRequestEvent', {'number': 42, 'pull_request': {'number': 42}})
        comment = self.event('IssueCommentEvent', {'issue': {'number': 42, 'pull_request': {'url': 'x'}}})
        self.assertEqual(pr['object_group'], comment['object_group'])
        self.assertEqual(summarize([pr, comment])['unique_object_groups'], 1)

    def test_review_group(self):
        row = self.event('PullRequestReviewEvent', {'pull_request': {'number': 42}})
        self.assertEqual(row['object_group'], 'owner/repo:pr:42')

    def test_push_group_not_commit_count(self):
        row = self.event('PushEvent', {'push_id': 5, 'commits': [{'sha': 'a'}, {'sha': 'b'}]})
        self.assertEqual(row['object_group'], 'owner/repo:push:5')
        self.assertEqual(summarize([row])['unique_object_groups'], 1)
        self.assertEqual(row['commit_entries'], 2)

    def test_star_is_not_business_object(self):
        row = self.event('WatchEvent', {'action': 'started'})
        self.assertIsNone(row['object_group'])
        self.assertEqual(summarize([row])['unique_object_groups'], 0)


if __name__ == '__main__':
    unittest.main()
