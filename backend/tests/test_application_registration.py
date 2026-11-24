"""Application-neutral admission, immutable history and dependency boundary regressions."""
import ast
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import digest
from app.services.task_inputs import registration, CONSOLE_ORIGIN
import test_runtime_task_console as fixtures

class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown=fixtures.TaskConsoleTests.asyncTearDown

    def register(self, *, governed=False):
        row={'id':'external-input','name':'Independent application','payload':{'opaque_key':'中文'}}
        row['sha256']=digest(row)
        self.data={'version':1,'requires_e2ag':governed,'inputs':[row],
            'schema':{'type':'object','required':['payload'],
                      'properties':{'payload':{'type':'object','required':['opaque_key']}}}}
        self.path=self.root/'external-inputs.json';self.save()
        profile=copy.deepcopy(self.profile);profile['id']='external'
        profile['console'].update(input_kind='example.external-task/v2',input_catalog=str(self.path))
        self.profile_path=self.profiles/'external.json';self.profile_path.write_text(json.dumps(profile))
        self.display=profile['console']
        return row

    def save(self):self.path.write_text(json.dumps(self.data,ensure_ascii=False))

    async def test_new_kind_without_application_import_or_core_edit(self):
        row=self.register()
        body=dict(profile_id='external',sample_id=row['id'],goal='opaque application task',idempotency_key='external')
        with patch('importlib.import_module',side_effect=AssertionError('No dynamic application import')):
            response=await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)
        self.assertEqual(response.status_code,202,response.text)
        run=response.json();self.assertEqual(run['sample'],row)
        self.assertEqual(run['task_kind'],'example.external-task/v2')
        self.assertEqual(run['state'],'queued')
        same=await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)
        self.assertEqual(same.json()['id'],run['id'])
        self.assertNotIn(str(self.path),(await self.client.get('/api/os/runtime/profiles',headers=self.headers)).text)
        self.assertEqual((await self.console.list())['items'][0]['id'],run['id'])

    async def test_catalog_update_and_removal_preserve_frozen_history(self):
        row=self.register();run=await self.console.submit('external',row['id'],'goal','stable')
        self.data['inputs'][0]['payload']['opaque_key']='changed'
        self.data['inputs'][0]['sha256']=digest({k:v for k,v in self.data['inputs'][0].items() if k!='sha256'});self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'IDEMPOTENCY'):
            await self.console.submit('external',row['id'],'goal','stable')
        newer=await self.console.submit('external',row['id'],'goal','new')
        self.profile_path.unlink();self.path.unlink()
        self.assertEqual((await self.console.detail(run['id']))['sample']['payload']['opaque_key'],'中文')
        self.assertEqual((await self.console.detail(newer['id']))['sample']['payload']['opaque_key'],'changed')
        self.assertEqual(len((await self.console.list())['items']),2)
        with self.assertRaisesRegex(RuntimeErrorCode,'PROFILE_NOT_AVAILABLE'):
            await self.console.submit('external',row['id'],'goal','third')

    async def test_schema_failure_invalid_digest_and_unregistered_input_never_queue(self):
        self.register()
        with self.assertRaisesRegex(RuntimeErrorCode,'SAMPLE_NOT_FOUND'):
            await self.console.submit('external','first-item','goal','bad')
        for change in ('digest','schema','duplicate'):
            old=copy.deepcopy(self.data)
            if change=='digest':self.data['inputs'][0]['payload']['opaque_key']='tampered'
            elif change=='schema':self.data['schema']['required']=['missing']
            else:self.data['inputs'].append(copy.deepcopy(self.data['inputs'][0]))
            self.save()
            with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_INPUT_CATALOG'):
                await self.console.submit('external','external-input','goal','bad')
            self.data=old;self.save()
        self.assertEqual((await self.console.list())['items'],[])

    async def test_e2ag_default_cannot_be_bypassed_by_renaming_kind(self):
        self.register(governed=True);del self.data['requires_e2ag'];self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'WORK_POLICY_REQUIRED'):
            await self.console.submit('external','external-input','goal','denied')
        self.assertEqual((await self.console.list())['items'],[])

    async def test_schema_never_resolves_network_files_or_changed_base_uri(self):
        self.register()
        for schema in ({'$ref':'https://example.invalid/schema'}, {'$ref':'file:///etc/passwd'},
                       {'$id':'https://example.invalid/','type':'object'},
                       {'$ref':'#/missing'}):
            self.data['schema']=schema;self.save()
            with patch('socket.socket',side_effect=AssertionError('No network')):
                with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_INPUT_CATALOG'):registration(self.display)
        self.data['schema']={'$defs':{'row':{'type':'object'}},'$ref':'#/$defs/row'};self.save()
        self.assertEqual(registration(self.display)['inputs'][0]['id'],'external-input')

    async def test_old_history_and_other_entrypoint_are_distinct(self):
        value={'runtime_profile':'review','task_kind':'inline-static-review',
               'payload':{'goal':'old','sample':{'id':'old','name':'old'}}}
        old=await self.console.store.submit('old',value)
        hidden=await self.console.store.submit('other',{**value,'origin':'other/v1'})
        self.assertEqual((await self.console.list())['items'][0]['id'],old['id'])
        self.assertEqual(len((await self.console.list())['items']),1)
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):await self.console.detail(hidden['id'])

    async def test_catalog_digest_covers_contract_and_input(self):
        self.register();first=registration(self.display)['digest']
        self.data['requires_e2ag']=True;self.save()
        self.assertNotEqual(first,registration(self.display)['digest'])
        # Application fields stay opaque; names such as project_id do not drive admission.
        row=self.data['inputs'][0];row['project_id']='arbitrary';row['sha256']=digest({k:v for k,v in row.items() if k!='sha256'});self.save()
        self.assertEqual(registration(self.display)['inputs'][0]['project_id'],'arbitrary')

class DynamicRegistrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=RegistrationTests.asyncSetUp
    asyncTearDown=RegistrationTests.asyncTearDown
    register=RegistrationTests.register
    save=RegistrationTests.save
    async def test_dynamic_input_is_schema_validated_frozen_and_idempotent(self):
        self.register()
        self.data['dynamic_inputs']=True;self.data['inputs']=[]
        self.data['schema']={'type':'object','required':['id','name','payload','sha256'],
            'properties':{'id':{'type':'string'},'name':{'type':'string'},'sha256':{'type':'string'},
                          'payload':{'type':'object','required':['opaque_key']}},'additionalProperties':False}
        self.save()
        data={'profile_id':'external','input_payload':{'id':'dynamic','name':'动态输入','payload':{'opaque_key':'原文'}},
              'goal':'goal','idempotency_key':'dynamic-1'}
        response=await self.client.post('/api/os/runtime/runs',headers=self.headers,json=data)
        self.assertEqual(response.status_code,202,response.text)
        saved=response.json();self.assertIn('sha256',saved['sample'])
        same=await self.client.post('/api/os/runtime/runs',headers=self.headers,json=data)
        self.assertEqual(same.json()['id'],saved['id'])
        data['input_payload']['payload']['opaque_key']='changed'
        self.assertEqual((await self.client.post('/api/os/runtime/runs',headers=self.headers,json=data)).status_code,409)
        data['idempotency_key']='dynamic-2';data['input_payload']['actor']='admin'
        self.assertEqual((await self.client.post('/api/os/runtime/runs',headers=self.headers,json=data)).status_code,422)
        self.assertEqual((await self.console.detail(saved['id']))['sample']['payload']['opaque_key'],'原文')
        self.assertEqual(len((await self.console.list())['items']),1)

    async def test_dynamic_requires_opt_in_and_exactly_one_input(self):
        self.register()
        body={'profile_id':'external','input_payload':{'id':'new','name':'new'},'goal':'goal','idempotency_key':'new'}
        response=await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)
        self.assertEqual(response.json()['detail'],'DYNAMIC_INPUT_NOT_ALLOWED')
        body['sample_id']='external-input'
        self.assertEqual((await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)).status_code,422)
        del body['sample_id'];del body['input_payload']
        self.assertEqual((await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)).status_code,422)
        self.data['dynamic_inputs']=True;del self.data['schema'];self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_INPUT_CATALOG'):self.console.catalog()

    async def test_dynamic_does_not_bypass_e2ag_or_digest(self):
        self.register(governed=True);self.data['dynamic_inputs']=True;self.save()
        row={'id':'new','name':'new','payload':{'opaque_key':'text'}}
        with self.assertRaisesRegex(RuntimeErrorCode,'WORK_POLICY_REQUIRED'):
            await self.console.submit('external',None,'goal','key',input_payload=row)
        row['sha256']='0'*64
        with self.assertRaisesRegex(RuntimeErrorCode,'INVALID_WORK_INPUT'):
            await self.console.submit('external',None,'goal','key',input_payload=row)
        self.assertEqual((await self.console.list())['items'],[])

class BoundaryTests(unittest.TestCase):
    def test_control_plane_does_not_import_reference_applications(self):
        root=Path(__file__).resolve().parents[1]/'app'
        for area in ['runtime','services','api']:
            for path in (root/area).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text())):
                    names=([node.module or ''] if isinstance(node,ast.ImportFrom)
                           else [alias.name for alias in node.names] if isinstance(node,ast.Import) else [])
                    self.assertFalse(any(name.startswith('app.apps') for name in names),str(path))
