"""Official MCP protocol and credential binding boundaries; no model calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from dios_knowledge_mcp.server import create_app


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.identity = dict(issuer='test', audience='runtime', scope='one', id='reader', subject='reader', actor='service')
        self.token = self.root / 'token'
        self.token.write_bytes(b'upstream-secret')
        self.binding = dict(token_sha256=hashlib.sha256(b'mcp-secret').hexdigest(), enabled=True,
            expires_at=time.time() + 300, runtime_token_file=str(self.token),
            runtime_token_sha256=hashlib.sha256(self.token.read_bytes()).hexdigest(), principal=self.identity)
        self.config = self.root / 'config.json'
        self.save()
        self.calls = []
        self.mode = 'principal'
        self.hook = lambda: None
        owner = self
        class Upstream:
            def __init__(self, url, token, **kwargs):
                owner.assertEqual(token, 'upstream-secret')
            def identity(self):
                return {'mode': owner.mode, 'principal': owner.identity}
            def search_knowledge(self, query, limit):
                owner.calls.append((query, limit))
                owner.hook()
                return {'contract': 'dios.knowledge/v1', 'items': []}
        self.patcher = patch('dios_knowledge_mcp.server.TaskClient', Upstream)
        self.patcher.start()
        self.client = TestClient(create_app(self.config))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patcher.stop()
        self.temp.cleanup()

    def save(self):
        self.config.write_text(json.dumps({'version': 1, 'runtime_url': 'http://runtime', 'bindings': [self.binding]}))

    def request(self, method='tools/call', params=None, token='mcp-secret', **headers):
        headers = {'Host': 'localhost:13187', 'Authorization': 'Bearer ' + token,
                   'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18', **headers}
        return self.client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 'test',
            'method': method, 'params': params or {'name': 'search', 'arguments': {'query': '中文'}}})

    def test_protocol_tool_list_schema_and_read_only_result(self):
        init = self.request('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(init.json()['result']['protocolVersion'], '2025-06-18')
        listed = self.request('tools/list', {}).json()['result']['tools']
        self.assertEqual([tool['name'] for tool in listed], ['search'])
        self.assertTrue(listed[0]['annotations']['readOnlyHint'])
        response = self.request()
        self.assertFalse(response.json()['result']['isError'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.calls, [('中文', 3)])

    def test_upstream_token_cannot_authenticate_mcp(self):
        self.assertEqual(self.request(token='upstream-secret').status_code, 403)
        self.assertEqual(self.calls, [])

    def test_operator_or_changed_credential_denied(self):
        self.mode = 'operator'
        self.assertEqual(self.request().status_code, 403)
        self.mode = 'principal'
        self.token.write_text('changed')
        self.assertEqual(self.request().status_code, 403)
        self.assertEqual(self.calls, [])

    def test_expired_and_disabled_binding_denied(self):
        self.binding['expires_at'] = 0
        self.save()
        self.assertEqual(self.request().status_code, 403)
        self.binding.update(expires_at=time.time() + 300, enabled=False)
        self.save()
        self.assertEqual(self.request().status_code, 403)

    def test_body_cannot_forge_query_identity(self):
        result = self.request(params={'name': 'search', 'arguments': {'query': '中文', 'actor': 'admin'}}).json()
        self.assertTrue(result['result']['isError'])
        self.assertEqual(self.calls, [])

    def test_binding_revoked_during_query_does_not_release_result(self):
        def revoke():
            self.binding['enabled'] = False
            self.save()
        self.hook = revoke
        result = self.request().json()['result']
        self.assertTrue(result['isError'])
        self.assertEqual(result['content'][0]['text'], 'KNOWLEDGE_ACCESS_DENIED')

    def test_untrusted_origin_denied(self):
        self.assertEqual(self.request(Origin='https://untrusted.example').status_code, 403)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
