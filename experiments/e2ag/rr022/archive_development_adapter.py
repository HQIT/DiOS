"""Narrow, offline Events API -> declared GitHub event bridge for development.

Only PushEvent, PR opened and non-PR issue opened are supported. Not a webhook
request, source-authentication mechanism, policy evaluator or general replay.
No text content, actor, credentials or network access is exported.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime


class AdaptationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise AdaptationError(message)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) is not None,
            'missing_or_invalid_commit_sha')
    require(value != '0' * 40, 'branch_creation_or_deletion_not_supported')
    return value


def positive_int(value):
    require(type(value) is int and value > 0, 'invalid_object_number')
    return value


def object_dict(value):
    require(isinstance(value, dict), 'invalid_object_shape')
    return value


def adapt(event):
    event = object_dict(event)
    require(event.get('public') is True, 'not_public_event')
    eid = event.get('id')
    require(isinstance(eid, str) and eid.isdecimal(), 'invalid_event_id')
    repo = object_dict(event.get('repo')).get('name')
    require(isinstance(repo, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) is not None,
            'invalid_repository_name')
    timestamp = event.get('created_at')
    require(isinstance(timestamp, str) and timestamp.endswith('Z'), 'invalid_event_time')
    try:
        datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError as exc:
        raise AdaptationError('invalid_event_time') from exc
    payload = object_dict(event.get('payload'))
    kind, action = event.get('type'), payload.get('action')
    bridge = {'repository': {'full_name': repo}}
    source_fields = {'event_id': eid, 'repository': repo, 'created_at': timestamp,
                     'event_type': kind, 'action': action}
    if kind == 'PushEvent':
        require(action in (None, ''), 'unsupported_action')
        ref = payload.get('ref')
        require(isinstance(ref, str) and ref.startswith('refs/heads/') and len(ref) > 11,
                'only_branch_push_supported')
        head, before = sha(payload.get('head')), sha(payload.get('before'))
        push_id = positive_int(payload.get('push_id'))
        bridge.update(ref=ref, before=before, after=head)
        source_fields.update(ref=ref, head=head, before=before, push_id=push_id)
        event_name, mapped_type = 'push', 'git.push'
        target = {'kind': 'push', 'repository': repo, 'push_id': push_id,
                  'ref': ref, 'before': before, 'head': head}
        version = head
        transforms = ['payload.head -> projected.after; original retained in source_fields']
    elif kind == 'PullRequestEvent':
        require(action == 'opened', 'unsupported_action')
        pr = object_dict(payload.get('pull_request'))
        number = positive_int(payload.get('number'))
        if pr.get('number') is not None:
            require(positive_int(pr['number']) == number, 'conflicting_pr_number')
        head = sha(object_dict(pr.get('head')).get('sha'))
        base = sha(object_dict(pr.get('base')).get('sha'))
        bridge.update(action='opened', number=number, pull_request={'number': number,
                      'head': {'sha': head}, 'base': {'sha': base}})
        source_fields.update(number=number, head=head, base=base)
        event_name, mapped_type = 'pull_request', 'git.pull_request.created'
        target = {'kind': 'pr', 'repository': repo, 'number': number, 'head': head, 'base': base}
        version = head
        transforms = ['outer payload.number -> projected.pull_request.number when absent; reject conflicting values']
    elif kind == 'IssuesEvent':
        require(action == 'opened', 'unsupported_action')
        issue = object_dict(payload.get('issue'))
        require('pull_request' not in issue, 'pr_not_plain_issue')
        number = positive_int(issue.get('number'))
        bridge.update(action='opened', issue={'number': number})
        # Exact archived issue snapshot hash is not a GitHub version identifier.
        snapshot = canonical_hash(issue)
        source_fields.update(number=number, issue_snapshot_sha256=snapshot)
        event_name, mapped_type = 'issues', 'git.issue.opened'
        target = {'kind': 'issue', 'repository': repo, 'number': number,
                  'snapshot_sha256': snapshot}
        version = snapshot
        transforms = ['issue snapshot -> canonical JSON SHA-256 (derived, not GitHub version)']
    else:
        raise AdaptationError('unsupported_event_type')
    return {'source_fields': source_fields, 'target': target, 'version': version,
            'projected_payload': bridge, 'normalizer_event_name': event_name,
            'expected_normalized_type': mapped_type, 'transformations': transforms,
            'trust': {'mode': 'local_archive_replay', 'original_webhook_signature_verified': False},
            'content_scope': 'metadata registration only; no code review, semantic summary or upstream writes'}


def make_task(adapted):
    """Task and policy are experimental constructs, never historical facts."""
    target = adapted['target']
    key = f"rr022-dev/{target['repository']}/{target['kind']}/{target.get('number', target.get('push_id'))}"
    return {
        'description': 'Register one source-attributed event snapshot in an isolated report store',
        'policy_origin': 'experiment_defined_not_upstream_repository_authorization',
        'policy': {'tool': 'event.record', 'resource': key, 'version': adapted['version'],
                   'source_event_id': adapted['source_fields']['event_id'], 'record_count': 1},
        'call': {'tool': 'event.record', 'arguments': {'resource': key, 'version': adapted['version'],
                 'source_event_id': adapted['source_fields']['event_id'], 'record_count': 1}},
        'expected_artifact': {'source': adapted['source_fields'], 'target': target,
                              'operation': 'register_event_metadata'},
    }


def oracle(policy, call):
    """Independent task contract, no imports from any evaluated method."""
    if not isinstance(call, dict) or set(call) != {'tool', 'arguments'}:
        return {'allow': False, 'reason': 'invalid_call'}
    if call['tool'] != policy['tool']:
        return {'allow': False, 'reason': 'tool_mismatch'}
    args = call['arguments']
    expected = {'resource', 'version', 'source_event_id', 'record_count'}
    if not isinstance(args, dict) or set(args) != expected:
        return {'allow': False, 'reason': 'field_set_mismatch'}
    for key in ('resource', 'version', 'source_event_id'):
        if type(args[key]) is not str or args[key] != policy[key]:
            return {'allow': False, 'reason': key + '_mismatch'}
    if type(args['record_count']) is not int or args['record_count'] != 1:
        return {'allow': False, 'reason': 'record_count_mismatch'}
    return {'allow': True, 'reason': 'contract_satisfied'}
