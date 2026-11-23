"""Knowledge ACL, version/citation integrity and registered business inputs."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from app.services.knowledge import FileKnowledgeProvider,version
from app.apps.evidence_brief import evidence_result,validate_input
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import digest
import test_runtime_task_console as console_fixtures

ROOT=Path(__file__).resolve().parents[2]

class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.catalog=json.loads((ROOT/'examples/business/catalog.json').read_text())
        self.input=json.loads((ROOT/'examples/business/sales-inputs.json').read_text())['inputs'][0]
        self.grants={'version':1,'grants':[dict(enabled=True,subject='sales-operator',project_id='sales-demo',
            token_sha256=hashlib.sha256(b'fixture-token').hexdigest(),documents={'sales-preparation':self.catalog['documents'][0]['digest']})]}
        self.provider=FileKnowledgeProvider(self.root/'catalog.json',self.root/'grants.json');self.save()
    def save(self):
        for name,value in [('catalog',self.catalog),('grants',self.grants)]:
            (self.root/(name+'.json')).write_text(json.dumps(value),encoding='utf-8')
    def search(self,query='配方'):return self.provider.search('fixture-token',{'query':query})
    def response(self):return {'isError':False,'content':[{'type':'text','text':json.dumps(self.search())}]}
    def test_access_before_search_and_no_metadata_leak(self):
        result=self.search();self.assertEqual([r['id'] for r in result['documents']],['sales-preparation'])
        self.assertEqual(self.search('RESTRICTED')['documents'],[])
        self.assertNotIn('construction-demo',json.dumps(result))
        with self.assertRaisesRegex(RuntimeErrorCode,'ACCESS_DENIED'):self.provider.search('wrong',{'query':'配方'})
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_TOOL_ARGUMENTS'):
            self.provider.search('fixture-token',{'query':'项目','project_id':'construction-demo'})
    def test_revocation_applies_to_next_read(self):
        self.search();self.grants['grants'][0]['enabled']=False;self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'ACCESS_DENIED'):self.search()
    def test_content_and_provenance_changes_require_new_version_grant(self):
        for field in ('text','source','locator','version','updated_at'):
            old=copy.deepcopy(self.catalog);row=self.catalog['documents'][0];row[field]+=' changed';row['digest']=version(row);self.save()
            with self.assertRaisesRegex(RuntimeErrorCode,'VERSION_CHANGED'):self.search()
            self.catalog=old;self.save()
    def test_inactive_and_missing_document_returns_no_evidence(self):
        self.catalog['documents'][0]['active']=False;self.save()
        self.assertEqual(self.search()['documents'],[])
        with self.assertRaisesRegex(RuntimeErrorCode,'EVIDENCE_INSUFFICIENT'):evidence_result(self.input,self.response(),'整理依据')
    def test_integrity_corruption_and_duplicate_ids_fail(self):
        self.catalog['documents'][0]['text']+=' altered';self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'VERSION_CHANGED'):self.search()
        self.catalog['documents'][0]['digest']=version(self.catalog['documents'][0]);self.grants['grants'][0]['documents']['sales-preparation']=self.catalog['documents'][0]['digest']
        self.catalog['documents'].append(self.catalog['documents'][0]);self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'CONFIGURATION_INVALID'):self.search()
    def test_report_retains_chinese_location_version_and_review_state(self):
        result=evidence_result(self.input,self.response(),'准备推荐依据')
        self.assertEqual(result['review_status'],'pending_review')
        self.assertIn('真实配方尚未接入',result['markdown'])
        self.assertEqual(result['citations'][0]['digest'],self.catalog['documents'][0]['digest'])
        self.assertEqual(result['citations'][0]['locator'],'examples/business/catalog.json#sales-preparation')
    def test_wrong_project_and_modified_quote_rejected(self):
        for field,value in [('project_id','other'),('text','invented')]:
            data=self.search();data['documents'][0][field]=value
            with self.assertRaisesRegex(RuntimeErrorCode,'EVIDENCE_INSUFFICIENT'):
                evidence_result(self.input,{'isError':False,'content':[{'type':'text','text':json.dumps(data)}]},'核查')
        with self.assertRaisesRegex(RuntimeErrorCode,'EVIDENCE_INSUFFICIENT'):
            evidence_result(self.input,{'isError':True,'content':[]},'核查')
    def test_metadata_and_quotes_cannot_inject_markdown_images(self):
        row=self.catalog['documents'][0];row['title']='![load](https://example.invalid)';row['text']+=' ![load](https://example.invalid)'
        row['digest']=version(row);self.grants['grants'][0]['documents'][row['id']]=row['digest'];self.save()
        result=evidence_result(self.input,self.response(),'![load](https://example.invalid)')
        self.assertNotIn('![load]',result['markdown'])
        self.assertIn('![load]',result['citations'][0]['text'])
    def test_same_query_different_identity_filters_project(self):
        row=self.catalog['documents'][1]
        self.grants['grants'].append(dict(enabled=True,subject='project-operator',project_id='construction-demo',
            token_sha256=hashlib.sha256(b'project-token').hexdigest(),documents={row['id']:row['digest']}));self.save()
        sales=self.provider.search('fixture-token',{'query':'工程样例'})
        construction=self.provider.search('project-token',{'query':'工程样例'})
        self.assertEqual(sales['documents'][0]['project_id'],'sales-demo')
        self.assertEqual(construction['documents'][0]['project_id'],'construction-demo')
    def test_input_changed_or_extra_field_rejected(self):
        for field,value in [('query','new query'),('project_id','other'),('actor','admin')]:
            altered={**self.input,field:value}
            with self.assertRaises(RuntimeErrorCode):validate_input(altered)

class BusinessConsoleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=console_fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown=console_fixtures.TaskConsoleTests.asyncTearDown
    async def test_business_catalog_and_profile_input_binding(self):
        p={**self.profile,'id':'knowledge','console':dict(enabled=True,input_kind='knowledge-evidence',name='Knowledge',
            input_catalog=str(ROOT/'examples/business/sales-inputs.json'))}
        (self.profiles/'knowledge.json').write_text(json.dumps(p))
        profiles=self.console.catalog();knowledge=next(x for x in profiles if x['id']=='knowledge')
        self.assertEqual(knowledge['input_ids'],['sales-evidence-demo'])
        self.assertEqual(len(self.console.samples()),3)
        with self.assertRaisesRegex(RuntimeErrorCode,'WORK_POLICY_REQUIRED'):
            await self.console.submit('knowledge','sales-evidence-demo','核查','demo')
        with self.assertRaisesRegex(RuntimeErrorCode,'SAMPLE_NOT_FOUND'):
            await self.console.submit('review','sales-evidence-demo','核查','demo')
        with self.assertRaisesRegex(RuntimeErrorCode,'SAMPLE_NOT_FOUND'):
            await self.console.submit('knowledge','first-item','核查','demo')
        self.assertNotIn(str(ROOT),(await self.client.get('/api/os/runtime/profiles',headers=self.headers)).text)
    async def test_business_task_uses_same_history_and_artifact_projection(self):
        value=json.loads((ROOT/'examples/business/sales-inputs.json').read_text())['inputs'][0]
        run=await self.console.store.submit('business-history',dict(runtime_profile='review',task_kind='knowledge-evidence',payload=dict(goal='核查',sample=value)))
        self.assertEqual((await self.console.detail(run['id']))['task_kind'],'knowledge-evidence')
        self.assertEqual((await self.console.list())['items'][0]['id'],run['id'])
