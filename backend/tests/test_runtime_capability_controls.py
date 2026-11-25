"""ACP callbacks cross the real Worker socket; admission rejects ungranted profiles."""
import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_runtime_task_console as console_fixture
import test_runtime_worker_tool_channel as channel_fixture
from app.runtime.database import RuntimeErrorCode
from app.runtime.work_authorizations import view
from app.runtime.worker import read_profile
from work_auth_fixtures import policy_fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'sdk/python/src'))
from dios_sdk.acp import AcpClient
from dios_sdk.cursor import proxy_permission
from dios_sdk.tools import ToolChannelClient


class Writer:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        pass


class CapabilitySocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = channel_fixture.WorkerToolChannelTests()
        await self.fixture.asyncSetUp()
        self.channel = self.fixture.channel
        await self.channel.open()
        self.writer = Writer()
        process = SimpleNamespace(stdin=self.writer, stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
        self.peer = AcpClient(process, tool_client=ToolChannelClient(str(self.channel.directory / 'channel.sock')),
                              permission_decoder=proxy_permission)
        self.peer.session_id = 'session-one'

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def invoke(self, method, params):
        await self.peer.incoming({'jsonrpc': '2.0', 'id': len(self.writer.messages) + 1,
                                 'method': method, 'params': params})
        return self.writer.messages[-1]

    async def test_unadvertised_files_and_all_terminal_methods_remain_denied(self):
        for method in ('fs/read_text_file', 'fs/write_text_file', 'terminal/create',
                       'terminal/output', 'terminal/wait_for_exit', 'terminal/kill', 'terminal/release'):
            result = await self.invoke(method, {'sessionId': 'session-one', 'path': '/synthetic/file'})
            self.assertEqual(result['error']['code'], -32601, method)
        f = self.fixture.fixture
        self.assertEqual(f.transport.calls, [])
        authorization = await view(f.store, f.run['id'])
        denied = [d for d in authorization['decisions'] if 'ACP_NATIVE_OPERATION_DENIED' in d['reason_codes']]
        self.assertEqual(len(denied), 7)

    async def test_agent_tool_claims_do_not_create_authority(self):
        options = [{'kind': 'allow_once', 'optionId': 'allow'}, {'kind': 'reject_once', 'optionId': 'deny'}]
        cases = [({'providerIdentifier': 'dios', 'toolName': 'search', 'args': {'query': 'evidence'}}, 'allow'),
                 ({'providerIdentifier': 'external', 'toolName': 'search', 'args': {'query': 'evidence'}}, 'deny'),
                 ({'providerIdentifier': 'dios', 'toolName': 'shell', 'args': {'command': 'build'}}, 'deny'),
                 ({'providerIdentifier': 'dios', 'toolName': 'search', 'args': {'command': 'build'}}, 'deny')]
        for raw, outcome in cases:
            result = await self.invoke('session/request_permission', {'sessionId': 'session-one',
                'toolCall': {'rawInput': raw}, 'options': options})
            self.assertEqual(result['result']['outcome']['optionId'], outcome)
        # The one allowed permission only enters the MCP proxy; it cannot dispatch.
        self.assertEqual(self.fixture.fixture.transport.calls, [])


class CapabilityAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = console_fixture.TaskConsoleTests.asyncSetUp
    asyncTearDown = console_fixture.TaskConsoleTests.asyncTearDown

    async def test_changed_control_boundary_requires_new_operator_admission(self):
        self.profile['governance'] = {'mode': 'e2ag'}
        path = self.profiles / 'review.json'
        path.write_text(json.dumps(self.profile))
        _, spec = read_profile(path)
        self.console.policy, _ = policy_fixture(self.root, 'review', spec)
        for field, value in [('image', 'unqualified-image'), ('command', ['other-harness']),
                             ('network', 'bridge'), ('read_only_root', False)]:
            changed = copy.deepcopy(self.profile)
            changed['launch'][field] = value
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(RuntimeErrorCode, 'WORK_PROFILE_CHANGED'):
                await self.console.submit('review', 'first-item', 'review', field)
        self.assertEqual((await self.console.list())['items'], [])

    async def test_unknown_harness_and_client_supplied_capabilities_never_queue(self):
        body = {'profile_id': 'not-admitted', 'sample_id': 'first-item', 'goal': 'review', 'idempotency_key': 'one'}
        response = await self.client.post('/api/os/runtime/runs', headers=self.headers, json=body)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail'], 'PROFILE_NOT_AVAILABLE')
        body.update(profile_id='review', capabilities={'terminal': True}, governance={'mode': 'e2ag'})
        response = await self.client.post('/api/os/runtime/runs', headers=self.headers, json=body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual((await self.console.list())['items'], [])

    async def test_legacy_compatibility_cannot_accept_governed_work(self):
        catalog = json.loads(self.input_catalog.read_text())
        catalog['requires_e2ag'] = True
        self.input_catalog.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(RuntimeErrorCode, 'WORK_POLICY_REQUIRED'):
            await self.console.submit('review', 'first-item', 'review', 'one')
        self.assertEqual((await self.console.list())['items'], [])
