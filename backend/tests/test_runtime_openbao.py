"""Pinned managed credentials, fault classification and E2AG execution gates."""
import asyncio,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from app.runtime.openbao_secrets import OpenBaoSecrets,validate_config,validate_reference
from app.runtime.oauth_source import OAuthSource,source_binding
from app.runtime.database import RuntimeErrorCode
import test_runtime_oauth_source as source

class ManagedFixture:
    def configure(self,root):
        source.SourceFixture.configure(self,root);self.config['timeout_seconds']=2
        self.sink=root/'bao-token';self.sink.write_text('service-token');self.sink.chmod(0o600)
        self.store_config={'address':'https://secrets.example','token_file':str(self.sink),'timeout_seconds':2}
        self.kv_status=200;self.kv_delay=0;self.kv_metadata={};self.kv_requests=[]
        self.config.pop('client_secret_file');self.config.update(secret_store=self.store_config,client_secret_ref={'mount':'identity','path':'client','field':'value','version':2})
        self.reference.pop('path');self.reference['secret_ref']={'mount':'identity','path':'source','field':'value','version':1}
        async def handle(request):
            self.kv_requests.append(request);await asyncio.sleep(self.kv_delay)
            if self.kv_status!=200:return httpx.Response(self.kv_status)
            version=1 if request.url.path.endswith('/source') else 2
            value=self.access if version==1 else self.secret
            return httpx.Response(200,json={'data':{'data':{'value':value},'metadata':{'version':version,'deletion_time':'','destroyed':False}|self.kv_metadata}})
        self.kv_transport=httpx.MockTransport(handle)
        actual=OpenBaoSecrets
        self.secret_override=patch('app.runtime.openbao_secrets.OpenBaoSecrets',side_effect=lambda cfg,**kwargs:actual(cfg,transport=self.kv_transport))
        self.secret_override.start();self.addCleanup(self.secret_override.stop)
        self.access_file.unlink();self.secret_file.unlink()

class ManagedSourceTests(ManagedFixture,unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.configure(Path(self.tmp.name))
        self.provider=OAuthSource(self.config,transport=self.transport)
    def test_fixed_versions_no_local_source_file_and_authlib(self):
        self.assertEqual(self.provider.current(self.reference)['subject'],'upstream-alice')
        self.assertEqual([x.url.params['version'] for x in self.kv_requests],['1','2'])
        self.assertTrue(all(x.headers['X-Vault-Token']=='service-token' for x in self.kv_requests))
        public=source_binding(self.reference,self.config);self.assertNotIn('secret_ref',public);self.assertNotIn('path',public)
    def test_sink_rotation_is_reread_without_changing_binding(self):
        binding=source_binding(self.reference,self.config);self.provider.current(self.reference);self.sink.write_text('replacement-token')
        self.provider.current(self.reference);self.assertEqual(self.kv_requests[-1].headers['X-Vault-Token'],'replacement-token');self.assertEqual(binding,source_binding(self.reference,self.config))
    def test_pinned_version_and_store_authority_changes_invalidate_binding(self):
        binding=source_binding(self.reference,self.config);self.reference['secret_ref']['version']=3;self.assertNotEqual(binding,source_binding(self.reference,self.config))
        self.reference['secret_ref']['version']=1;self.config['secret_store']['address']='https://other.example';self.assertNotEqual(binding,source_binding(self.reference,self.config))
    def test_deleted_source_revoked_but_service_failure_unavailable(self):
        self.kv_status=404
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):self.provider.current(self.reference)
        for status in (302,403,429,503):
            self.kv_status=status
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):self.provider.current(self.reference)
        self.assertEqual(self.requests,[])
    def test_missing_client_secret_is_outage_and_changed_source_is_revoked(self):
        original=self.kv_transport
        async def missing_client(request):
            if request.url.path.endswith('/client'):return httpx.Response(404)
            return await original.handle_async_request(request)
        self.kv_transport=httpx.MockTransport(missing_client)
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):self.provider.current(self.reference)
        self.kv_transport=original;self.access='changed-source-value'
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):self.provider.current(self.reference)
        self.assertEqual(self.requests,[])
    def test_wrong_version_and_bad_metadata_fail_closed(self):
        for metadata in ({'version':99},{'version':True},{'destroyed':None},{'deletion_time':'invalid'},{'deletion_time':'2026-11-24T00:00:00'}):
            self.kv_metadata=metadata
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE|AUTH_PRINCIPAL_REVOKED'):self.provider.current(self.reference)
        for metadata in ({'destroyed':True},{'deletion_time':'2026-11-24T00:00:00Z'}):
            self.kv_metadata=metadata
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):self.provider.current(self.reference)
    def test_scheduled_future_deletion_is_usable_until_due(self):
        self.kv_metadata={'deletion_time':'2099-01-01T00:00:00Z'}
        self.assertEqual(self.provider.current(self.reference)['subject'],'upstream-alice')
    def test_total_source_deadline_includes_secret_reads(self):
        self.config['timeout_seconds']=.2;self.kv_delay=.12;start=time.monotonic()
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):self.provider.current(self.reference)
        self.assertLess(time.monotonic()-start,.5);self.assertEqual(self.requests,[])
    def test_response_bounds_and_malformed_json(self):
        for content in (b'not-json',b'{"padding":"'+b'x'*65536+b'"}'):
            reader=OpenBaoSecrets(self.store_config,transport=httpx.MockTransport(lambda req:httpx.Response(200,content=content)))
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):reader.read(self.reference['secret_ref'])
    def test_config_rejects_unpinned_ambiguous_and_escaping_references(self):
        for value in (0,True,-1,'latest'):
            with self.assertRaises(ValueError):validate_reference(self.reference['secret_ref']|{'version':value})
        for path in ('../source','a//source','a%2fsource','a?version=2','/source'):
            with self.assertRaises(ValueError):validate_reference(self.reference['secret_ref']|{'path':path})
        for address in (None,42,False,['https://secrets.example'],'http://remote.example','https://u:p@secrets.example','https://secrets.example/path','https://secrets.example?token=a'):
            with self.assertRaises(ValueError):validate_config(self.store_config|{'address':address})
        with self.assertRaises(ValueError):source.validate_provider(self.config|{'client_secret_file':str(self.secret_file)})

class ManagedPrincipalTests(ManagedFixture,source.OAuthPrincipalTests):
    async def test_store_outage_recovery_and_deleted_version_cannot_resurrect_work(self):
        run=await self.submit_as('a');context=await self.context(run);await self.verifier(context,'execute')
        self.kv_status=503
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):await self.verifier(context,'execute')
        self.kv_status=200;await self.verifier(context,'execute')
        self.kv_status=404
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.verifier(context,'execute')
        self.kv_status=200
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.verifier(context,'execute')

class ManagedToolTests(ManagedFixture,source.OAuthToolTests):
    async def test_deleted_source_after_approval_blocks_call_and_undelete_does_not_revive(self):
        item=await self.approved();self.kv_status=404
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.kv_status=200
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.action_transport.calls,[])
