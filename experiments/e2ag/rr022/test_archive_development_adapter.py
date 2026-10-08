import copy
import unittest
from archive_development_adapter import adapt, make_task, oracle, AdaptationError, canonical_hash


def fixture(kind='PullRequestEvent'):
    payloads = {
        'PullRequestEvent': {'action': 'opened', 'number': 7, 'pull_request': {
            'number': 7, 'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}, 'body': 'untrusted-text'}},
        'PushEvent': {'ref': 'refs/heads/main', 'head': 'a' * 40, 'before': 'b' * 40, 'push_id': 17},
        'IssuesEvent': {'action': 'opened', 'issue': {'number': 9, 'body': 'untrusted-text'}},
    }
    return {'id': '123', 'public': True, 'type': kind, 'created_at': '2026-09-21T12:00:00Z',
            'repo': {'name': 'org/repo'}, 'payload': copy.deepcopy(payloads[kind]),
            'actor': {'login': 'not-exported'}}


class AdapterTests(unittest.TestCase):
    def test_push_explicit_head_translation(self):
        result = adapt(fixture('PushEvent'))
        self.assertEqual(result['projected_payload']['after'], 'a' * 40)
        self.assertEqual(result['source_fields']['head'], 'a' * 40)

    def test_pr_source_fields_retained(self):
        result = adapt(fixture())
        self.assertEqual(result['target'], {'kind': 'pr', 'repository': 'org/repo', 'number': 7,
                                           'head': 'a' * 40, 'base': 'b' * 40})

    def test_issue_canonical_snapshot(self):
        event = fixture('IssuesEvent')
        result = adapt(event)
        self.assertEqual(result['version'], canonical_hash(event['payload']['issue']))
        event['payload']['issue']['body'] = 'changed'
        self.assertNotEqual(result['version'], adapt(event)['version'])

    def test_no_body_or_actor_export(self):
        for kind in ('PullRequestEvent', 'IssuesEvent'):
            value = str(adapt(fixture(kind)))
            self.assertNotIn('untrusted-text', value)
            self.assertNotIn('not-exported', value)

    def test_never_claims_original_signature(self):
        self.assertFalse(adapt(fixture())['trust']['original_webhook_signature_verified'])

    def test_non_public_rejected(self):
        event = fixture(); event['public'] = False
        with self.assertRaisesRegex(AdaptationError, 'not_public'):
            adapt(event)

    def test_unknown_type_explicit(self):
        event = fixture(); event['type'] = 'WatchEvent'
        with self.assertRaisesRegex(AdaptationError, 'unsupported_event_type'):
            adapt(event)

    def test_nonopened_not_silently_mapped(self):
        event = fixture(); event['payload']['action'] = 'merged'
        with self.assertRaisesRegex(AdaptationError, 'unsupported_action'):
            adapt(event)

    def test_missing_sha_not_filled_from_current_state(self):
        event = fixture(); del event['payload']['pull_request']['head']['sha']
        with self.assertRaisesRegex(AdaptationError, 'commit_sha'):
            adapt(event)

    def test_conflicting_pr_number(self):
        event = fixture(); event['payload']['pull_request']['number'] = 8
        with self.assertRaisesRegex(AdaptationError, 'conflicting_pr_number'):
            adapt(event)

    def test_pr_number_may_be_explicitly_derived(self):
        event = fixture(); del event['payload']['pull_request']['number']
        self.assertEqual(adapt(event)['projected_payload']['pull_request']['number'], 7)

    def test_boolean_not_object_number(self):
        event = fixture(); event['payload']['number'] = True
        with self.assertRaisesRegex(AdaptationError, 'object_number'):
            adapt(event)

    def test_pr_issue_confusion(self):
        event = fixture('IssuesEvent'); event['payload']['issue']['pull_request'] = {}
        with self.assertRaisesRegex(AdaptationError, 'pr_not_plain_issue'):
            adapt(event)

    def test_deleted_branch_not_regular_push(self):
        event = fixture('PushEvent'); event['payload']['head'] = '0' * 40
        with self.assertRaisesRegex(AdaptationError, 'creation_or_deletion'):
            adapt(event)

    def test_event_time_preserved_and_required(self):
        self.assertEqual(adapt(fixture())['source_fields']['created_at'], '2026-09-21T12:00:00Z')
        event = fixture(); event['created_at'] = 'not-a-timeZ'
        with self.assertRaisesRegex(AdaptationError, 'event_time'):
            adapt(event)


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.task = make_task(adapt(fixture()))

    def test_normal(self):
        self.assertTrue(oracle(self.task['policy'], self.task['call'])['allow'])

    def test_each_binding_separately(self):
        for key in ('resource', 'version', 'source_event_id'):
            with self.subTest(field=key):
                call = copy.deepcopy(self.task['call']); call['arguments'][key] = 'different'
                self.assertEqual(oracle(self.task['policy'], call), {'allow': False, 'reason': key + '_mismatch'})

    def test_extra_fields_rejected(self):
        call = copy.deepcopy(self.task['call']); call['arguments']['extra'] = 'x'
        self.assertEqual(oracle(self.task['policy'], call)['reason'], 'field_set_mismatch')

    def test_wrong_tool(self):
        call = copy.deepcopy(self.task['call']); call['tool'] = 'another.tool'
        self.assertEqual(oracle(self.task['policy'], call)['reason'], 'tool_mismatch')

    def test_strict_count_type(self):
        for value in (True, 1.0, '1', 0, 2):
            call = copy.deepcopy(self.task['call']); call['arguments']['record_count'] = value
            self.assertFalse(oracle(self.task['policy'], call)['allow'])


if __name__ == '__main__':
    unittest.main()
