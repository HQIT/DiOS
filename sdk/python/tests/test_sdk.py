import hashlib
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from dios_sdk import TaskClient,ApiError,TransportError,ObservationTimeout,WorkloadError,digest,encoded,emit,work_input

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.requests=[];self.status=200;self.body={'state':'running'};self.headers={}
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.respond()
            def do_POST(self):self.respond()
            def respond(self):
                raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
                owner.requests.append((self.command,self.path,json.loads(raw) if raw else None,self.headers.get('Authorization')))
                value=owner.body if isinstance(owner.body,bytes) else json.dumps(owner.body,ensure_ascii=False).encode()
                self.send_response(owner.status)
                for k,v in owner.headers.items():self.send_header(k,v)
                self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.client=TaskClient(self.url,'private-token',timeout=.2)
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def test_registered_and_dynamic_submission_preserve_keys_and_input(self):
        self.client.submit('profile','input','中文目标',idempotency_key='stable')
        self.client.submit_input('profile',{'id':'input','name':'中文','payload':{'opaque':1}},'goal',idempotency_key='dynamic')
        self.assertEqual(self.requests[0][2]['idempotency_key'],'stable')
        self.assertEqual(self.requests[1][2]['input_payload']['payload'],{'opaque':1})
        self.assertNotIn('sample_id',self.requests[1][2])
        self.assertEqual(self.requests[0][3],'Bearer private-token')
    def test_error_details_and_redirect_do_not_leak_or_forward_credentials(self):
        self.status=403;self.body={'detail':'secret private-token'}
        with self.assertRaises(ApiError) as caught:self.client.get('one')
        self.assertEqual(str(caught.exception),'HTTP_ERROR')
        self.status=302;self.headers={'Location':self.url+'/redirect-target'};self.body={}
        with self.assertRaises(ApiError) as caught:self.client.get('two')
        self.assertEqual(caught.exception.status,302)
        self.assertEqual(len(self.requests),2)
        self.assertFalse(any('redirect-target' in x[1] for x in self.requests))
    def test_profile_precondition_forwarded_for_both_submission_modes(self):
        fingerprint='a'*64
        self.client.submit('profile','input','goal',idempotency_key='pinned',expected_profile_digest=fingerprint)
        self.client.submit_input('profile',{'id':'input'},'goal',idempotency_key='dynamic',expected_profile_digest=fingerprint)
        self.assertTrue(all(row[2]['expected_profile_digest']==fingerprint for row in self.requests))
    def test_delegated_submissions_bind_same_reference_and_key_on_retry(self):
        for _ in range(2):
            self.client.submit('profile','input','goal',idempotency_key='same',delegation_ref='dl-one',parent_run_id='parent-one')
        self.client.submit_input('profile',{'id':'input','name':'中文'},'goal',idempotency_key='dynamic',delegation_ref='dl-two',parent_run_id='parent-two')
        self.assertEqual(self.requests[0][2],self.requests[1][2])
        self.assertEqual(self.requests[0][2]['delegation_ref'],'dl-one')
        self.assertEqual(self.requests[2][2]['delegation_ref'],'dl-two')
        self.assertEqual(self.requests[0][2]['parent_run_id'],'parent-one')
        self.assertEqual(self.requests[2][2]['parent_run_id'],'parent-two')
    def test_query_timeout_keeps_same_task_and_never_resubmits(self):
        self.body={'state':'running','id':'existing'}
        with self.assertRaises(ObservationTimeout) as caught:self.client.wait('existing',timeout=.03,interval=.01)
        self.assertEqual(caught.exception.run_id,'existing')
        self.assertEqual(caught.exception.last_observation['state'],'running')
        self.assertTrue(all(method=='GET' and path.endswith('/existing') for method,path,_,_ in self.requests))
    def test_transport_failure_retains_submission_ambiguity_and_key(self):
        with patch.object(self.client._opener,'open',side_effect=TimeoutError('private-token')):
            with self.assertRaises(TransportError) as caught:self.client.submit('p','i','g',idempotency_key='same')
        self.assertTrue(caught.exception.ambiguous);self.assertEqual(caught.exception.idempotency_key,'same')
        self.assertNotIn('private-token',str(caught.exception))
    def test_identity_uses_authenticated_runtime_endpoint(self):
        self.body={'mode':'principal','principal':{'id':'one'}}
        self.assertEqual(self.client.identity()['principal']['id'],'one')
        self.assertTrue(self.requests[-1][1].endswith('/identity'))
        self.assertEqual(self.requests[-1][3],'Bearer private-token')
    def test_delegation_transport_keeps_identity_server_owned_and_ambiguous_key(self):
        args=('root','profile','actor:target',[],12345)
        self.client.issue_delegation(*args,idempotency_key='stable-delegation',parent_run_id='parent-one')
        self.assertEqual(self.requests[-1][2]['parent_run_id'],'parent-one')
        self.assertEqual(self.requests[-1][1],'/api/os/runtime/delegations')
        self.assertNotIn('issuer_actor',self.requests[-1][2])
        self.assertEqual(self.requests[-1][3],'Bearer private-token')
        self.client.delegation('ref/one');self.client.revoke_delegation('ref/one',expected_version=2)
        self.assertTrue(self.requests[-1][1].endswith('/ref%2Fone/revoke'))
        self.assertEqual(self.requests[-1][2],{'expected_version':2})
        with patch.object(self.client._opener,'open',side_effect=TimeoutError()):
            with self.assertRaises(TransportError) as caught:self.client.issue_delegation(*args,idempotency_key='stable-delegation')
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(caught.exception.idempotency_key,'stable-delegation')
    def test_artifact_size_and_digest_are_verified(self):
        raw='中文结果🙂'.encode();record={'id':'a','digest':hashlib.sha256(raw).hexdigest(),'size':len(raw)}
        with patch.object(self.client,'get',return_value={'artifacts':[record]}):
            self.body=raw;self.assertEqual(self.client.artifact('r','a'),raw)
            self.body=b'corrupt'
            with self.assertRaisesRegex(WorkloadError,'DIGEST_MISMATCH'):self.client.artifact('r','a')
    def test_url_limits_and_path_escaping(self):
        for url in ['http://external.invalid','https://user:password@example.invalid','https://example.invalid/?token=x']:
            with self.assertRaises(ValueError):TaskClient(url,'token')
        self.client.get('id/with?query')
        self.assertIn('id%2Fwith%3Fquery',self.requests[-1][1])
        self.body=b'x'*1025;small=TaskClient(self.url,'token',max_bytes=1024)
        with self.assertRaisesRegex(WorkloadError,'TOO_LARGE'):small.get('x')

class ProtocolTests(unittest.TestCase):
    def test_utf8_canonical_input_and_jsonl_boundaries(self):
        value={'字':'🙂','a':1};out=io.BytesIO();emit({'type':'output','text':'中文'},stream=out)
        self.assertEqual(json.loads(out.getvalue()),{'type':'output','text':'中文'})
        self.assertEqual(digest(value),digest({'a':1,'字':'🙂'}))
        self.assertEqual(work_input({'DIOS_WORK_INPUT':json.dumps(value)}),value)
        with self.assertRaises(WorkloadError):work_input({'DIOS_WORK_INPUT':'[1]'})
        with self.assertRaises(WorkloadError):work_input({'DIOS_WORK_INPUT':'{"n":NaN}'})
        with self.assertRaisesRegex(WorkloadError,'OUTPUT_LIMIT'):emit({'text':'字'*65537},stream=out)
