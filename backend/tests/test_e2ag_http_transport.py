import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.api.internal.e2ag_mcp import proxy_mcp_call
from app.services.e2ag_http_transport import BorrowedTransport, mcp_transport_lifespan, transport_options
import test_e2ag_tool_gateway as gateway_tests


@contextmanager
def echo_server():
    records = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            record = {key:self.headers.get(key) for key in ('authorization', 'mcp-session-id', 'cookie')}
            record['port'] = self.client_address[1]
            records.append(record)
            body = json.dumps(record).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Set-Cookie', 'private_task_cookie=must-not-carry; Path=/')
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/mcp', records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_and_missing_app_fall_back_to_per_call(self):
        app = FastAPI()
        async with mcp_transport_lifespan(app, enabled=False):
            self.assertEqual({}, transport_options(SimpleNamespace(scope={'app': app})))
        self.assertEqual({}, transport_options(SimpleNamespace(scope={})))

    async def test_owner_closes_pool_even_on_exception_with_safe_options(self):
        class Transport(httpx.AsyncBaseTransport):
            closed = False
            async def aclose(self):
                self.closed = True
        pool, app = Transport(), FastAPI()
        with patch('app.services.e2ag_http_transport.httpx.AsyncHTTPTransport', return_value=pool) as factory:
            with self.assertRaisesRegex(RuntimeError, 'test shutdown'):
                async with mcp_transport_lifespan(app, enabled=True):
                    wrapper = transport_options(SimpleNamespace(scope={'app': app}))['transport']
                    await wrapper.aclose()
                    self.assertFalse(pool.closed)
                    raise RuntimeError('test shutdown')
            self.assertTrue(pool.closed)
            self.assertFalse(hasattr(app.state, 'e2ag_mcp_transport'))
            options = factory.call_args.kwargs
            self.assertIs(options['verify'], True)
            self.assertIs(options['trust_env'], False)
            self.assertEqual(0, options['retries'])
            self.assertEqual(100, options['limits'].max_connections)
            self.assertEqual(20, options['limits'].max_keepalive_connections)
            self.assertEqual(5, options['limits'].keepalive_expiry)

    async def test_real_socket_reused_without_cookie_or_identity_reuse(self):
        app = FastAPI()
        with echo_server() as (url, records):
            async with mcp_transport_lifespan(app, enabled=True):
                for suffix in ('one', 'two'):
                    async with httpx.AsyncClient(trust_env=False, timeout=30, follow_redirects=True,
                                                 **transport_options(SimpleNamespace(scope={'app': app}))) as client:
                        result = await client.post(url, json={}, headers={'Authorization': 'Bearer '+suffix, 'Mcp-Session-Id': suffix})
                        self.assertEqual(200, result.status_code)
            self.assertEqual(2, len(records))
            self.assertEqual(records[0]['port'], records[1]['port'])
            self.assertEqual(['Bearer one', 'Bearer two'], [r['authorization'] for r in records])
            self.assertEqual(['one', 'two'], [r['mcp-session-id'] for r in records])
            self.assertTrue(all(r['cookie'] is None for r in records))

    async def test_concurrent_request_headers_remain_independent(self):
        app = FastAPI()
        with echo_server() as (url, records):
            async with mcp_transport_lifespan(app, enabled=True):
                async def call(i):
                    async with httpx.AsyncClient(trust_env=False, timeout=30,
                                                 **transport_options(SimpleNamespace(scope={'app': app}))) as client:
                        response = await client.post(url, json={}, headers={'Authorization': f'Bearer {i}', 'Mcp-Session-Id': str(i)})
                        value = response.json()
                        self.assertEqual(f'Bearer {i}', value['authorization'])
                        self.assertEqual(str(i), value['mcp-session-id'])
                        self.assertIsNone(value['cookie'])
                await asyncio.gather(*(call(i) for i in range(8)))
            self.assertEqual(8, len(records))

    async def test_transport_timeout_does_not_resend(self):
        calls = []
        async def fail(request):
            calls.append(request)
            raise httpx.ReadTimeout('unknown result')
        async with httpx.MockTransport(fail) as pool:
            async with httpx.AsyncClient(transport=BorrowedTransport(pool)) as client:
                with self.assertRaises(httpx.ReadTimeout):
                    await client.post('http://example.test/', json={})
        self.assertEqual(1, len(calls))


class SharedProxyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gateway_tests.E2AGToolGatewayTests.asyncSetUp
    asyncTearDown = gateway_tests.E2AGToolGatewayTests.asyncTearDown
    _fixtures = gateway_tests.E2AGToolGatewayTests._fixtures

    async def test_shared_proxy_still_checks_each_token_and_tool(self):
        calls = []
        async def upstream(request):
            calls.append(request)
            self.assertEqual('secret', request.headers['x-upstream-key'])
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(200, json={'jsonrpc':'2.0', 'id':1, 'result':{'ok':True}}, headers={'Set-Cookie':'secret=private; Path=/'})
        app = FastAPI()
        async with httpx.MockTransport(upstream) as pool, self.sessions() as db:
            app.state.e2ag_mcp_transport = pool
            *_, grant, token, _ = await self._fixtures(db)
            for presented, tool, status in [(token, 'ledger.record_push', 200), ('wrong', 'ledger.record_push', 403),
                                             (token, 'admin.delete_all', 403), (token, 'ledger.record_push', 200)]:
                request = gateway_tests.request_for({'jsonrpc':'2.0', 'id':1, 'method':'tools/call', 'params':{'name':tool, 'arguments':{}}}, presented)
                request.scope['app'] = app
                self.assertEqual(status, (await proxy_mcp_call(grant.id, request, db)).status_code)
            self.assertEqual(2, len(calls))
            self.assertNotIn('cookie', calls[1].headers)
