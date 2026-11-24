import asyncio
import json
import unittest
import httpx
from app.runtime.policy_evaluator import OpaEvaluator,validate
from app.runtime.database import RuntimeErrorCode

CONFIG={'kind':'opa','url':'http://127.0.0.1:8181/v1/data/dios/allow','bundles':{'tools':'v1'},'timeout_seconds':.1}

class PolicyEvaluatorTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self,payload,status=200,headers=None):
        async def handle(request):
            self.assertIn('provenance=true',str(request.url))
            self.assertEqual(json.loads(request.content),{'input':{'trusted':'binding'}})
            return httpx.Response(status,json=payload,headers=headers)
        return await OpaEvaluator(transport=httpx.MockTransport(handle)).evaluate(CONFIG,{'trusted':'binding'})
    async def test_true_false_and_native_bundle_provenance(self):
        for allow in (True,False):
            value=await self.evaluate({'result':allow,'provenance':{'bundles':{'tools':{'revision':'v1'}}},'decision_id':'fixture'})
            self.assertIs(value['allow'],allow);self.assertEqual(value['bundles'],{'tools':'v1'})
            self.assertEqual(len(value['input_digest']),64)
    async def test_undefined_truthy_malformed_and_wrong_revision_denied(self):
        for payload,code in [({},'POLICY_RESPONSE_INVALID'),({'result':'true'},'POLICY_RESPONSE_INVALID'),
            ({'result':True},'POLICY_PROVENANCE_REQUIRED'),
            ({'result':True,'provenance':{'bundles':{'tools':{'revision':'v2'}}}},'POLICY_REVISION_CHANGED'),
            ({'result':True,'provenance':{'bundles':{'tools':{'revision':'v1'},'injected':{'revision':'v1'}}}},'POLICY_REVISION_CHANGED')]:
            with self.assertRaisesRegex(RuntimeErrorCode,code):await self.evaluate(payload)
    async def test_redirect_and_http_error_do_not_allow_or_forward(self):
        for status in (302,403,500):
            with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_EVALUATOR_UNAVAILABLE'):
                await self.evaluate({},status,{'Location':'https://untrusted.invalid'})
    async def test_total_deadline_and_response_bound(self):
        async def slow(request):await asyncio.sleep(.2);return httpx.Response(200,json={'result':True})
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_EVALUATOR_TIMEOUT'):
            await OpaEvaluator(transport=httpx.MockTransport(slow)).evaluate(CONFIG,{})
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_RESPONSE_TOO_LARGE'):await self.evaluate({'padding':'x'*65536})
    def test_endpoint_and_configuration_constraints(self):
        for change in ({'url':'http://external.invalid/v1/data/allow'},{'url':'https://a:b@example.com/v1/data/allow'},
            {'url':'http://localhost/admin'},{'url':'http://localhost/v1/data/allow?q=1'},{'bundles':{}},{'timeout_seconds':10}):
            with self.assertRaises(RuntimeErrorCode):validate(dict(CONFIG,**change))
