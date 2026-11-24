"""Deferred acceptance coverage for the operator task flow; no provider calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
import httpx
from app.api.os import runtime_tasks
from app.runtime.adapter import LaunchSpec
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.services.task_console import TaskConsole
import sys
ROOT=Path(__file__).resolve().parents[2]
for source in ['sdk/python/src','applications/static-review/src']:sys.path.insert(0,str(ROOT/source))
from dios_review.app import sample, review_result
from dios_sdk import WorkloadError
from app.runtime.worker import Worker
from test_runtime_worker import FakeAdapter


class TaskConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=Database('sqlite+aiosqlite:///'+str(self.root/'runtime.sqlite'));await self.db.migrate()
        self.profiles=self.root/'profiles';self.profiles.mkdir()
        catalog=json.loads((Path(__file__).resolve().parents[2]/'examples/runtime/review-inputs.json').read_text())
        catalog['requires_e2ag']=False
        self.input_catalog=self.root/'review-inputs.json';self.input_catalog.write_text(json.dumps(catalog))
        self.profile=dict(id='review' ,bridge='jsonl-v1',
            console=dict(enabled=True,input_kind='inline-static-review',input_catalog=str(self.input_catalog),name='Test Agent',implementation='test',synthetic=True),
            launch=dict(image='test-image',command=['test-command'],environment={'PRIVATE_EXAMPLE':'do-not-return'}))
        (self.profiles/'review.json').write_text(json.dumps(self.profile))
        self.console=TaskConsole(self.db,'one',self.profiles,self.root/'objects')
        self.app=FastAPI();self.app.include_router(runtime_tasks.router,prefix='/api/os')
        self.app.state.runtime_console=self.console
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')
        self.enabled=patch.object(runtime_tasks.settings,'runtime_console_enabled',True);self.enabled.start()
        self.token=patch.object(runtime_tasks.settings,'access_token','operator-test');self.token.start()
        self.headers={'X-DiOS-Access-Token':'operator-test'}

    async def asyncTearDown(self):
        await self.client.aclose();self.token.stop();self.enabled.stop();await self.db.close();self.temp.cleanup()

    async def submit(self,key='one'):
        return await self.console.submit('review','first-item','检查边界条件',key)

    async def finish(self,run):
        value=sample('first-item')
        result=review_result(value,dict(summary='空列表会触发异常',findings=[dict(title='空列表下标越界',
            line_start=2,line_end=2,condition='items 为空列表',evidence='items[0]',impact='抛出 IndexError')]))
        data=b''.join((json.dumps(m,ensure_ascii=False)+'\n').encode() for m in [
            {'type':'session','session_id':'real-session'}, {'type':'output','text':'正在检查中文证据'},
            {'type':'result','session_id':'real-session','result':result}])
        spec=LaunchSpec('test-image',('test-command',),environment={'PRIVATE_EXAMPLE':'do-not-return'})
        await Worker(self.console.store,FakeAdapter(data),'review',spec,self.console.provider).once()
        return await self.console.detail(run['id'])

    async def test_route_requires_operator_token_and_explicit_enable(self):
        self.assertEqual((await self.client.get('/api/os/runtime/profiles')).status_code,401)
        response=await self.client.get('/api/os/runtime/profiles',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('PRIVATE_EXAMPLE',response.text);self.assertNotIn('test-command',response.text)
        with patch.object(runtime_tasks.settings,'runtime_console_enabled',False):
            self.assertEqual((await self.client.get('/api/os/runtime/profiles',headers=self.headers)).status_code,503)

    async def test_submit_idempotent_and_http_never_launches(self):
        body=dict(profile_id='review',sample_id='first-item',goal='检查边界条件',idempotency_key='one')
        first=await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)
        second=await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)
        self.assertEqual(first.status_code,202);self.assertEqual(first.json()['id'],second.json()['id'])
        self.assertEqual(first.json()['state'],'queued');self.assertEqual(first.json()['attempts'],[])
        body['goal']='不同内容'
        self.assertEqual((await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)).status_code,409)
        body['command']='unsafe'
        self.assertEqual((await self.client.post('/api/os/runtime/runs',headers=self.headers,json=body)).status_code,422)

    async def test_only_registered_profiles_and_samples_accepted(self):
        with self.assertRaisesRegex(RuntimeErrorCode,'PROFILE_NOT_AVAILABLE'):
            await self.console.submit('../../profile','first-item','goal','one')
        with self.assertRaisesRegex(RuntimeErrorCode,'SAMPLE_NOT_FOUND'):
            await self.console.submit('review','../../source','goal','one')

    async def test_end_to_end_projection_preview_and_authorized_download(self):
        r=await self.finish(await self.submit())
        self.assertEqual(r['state'],'succeeded');self.assertEqual(r['attempts'][0]['output_excerpt'],'正在检查中文证据')
        artifact=next(a for a in r['artifacts'] if a['kind']=='result')
        path=f'/api/os/runtime/runs/{r["id"]}/artifacts/{artifact["id"]}'
        self.assertEqual((await self.client.get(path+'/download')).status_code,401)
        preview=await self.client.get(path+'/preview',headers=self.headers)
        self.assertIn('静态评审报告',preview.json()['markdown'])
        download=await self.client.get(path+'/download?format=markdown',headers=self.headers)
        self.assertEqual(download.status_code,200);self.assertIn('空列表',download.text)
        self.assertEqual(download.headers['x-content-type-options'],'nosniff')
        self.assertNotIn('storage_key',json.dumps(r));self.assertNotIn('PRIVATE_EXAMPLE',json.dumps(r))

    async def test_cross_scope_and_cross_run_artifact_access_denied(self):
        r=await self.finish(await self.submit());other=await self.submit('two')
        artifact=r['artifacts'][0]
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):
            await self.console.artifact(other['id'],artifact['id'])
        wrong=TaskConsole(self.db,'other',self.profiles,self.root/'objects')
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):await wrong.detail(r['id'])

    async def test_stable_pagination_and_cancel_revision(self):
        one,two=await self.submit('one'),await self.submit('two')
        first=await self.console.list(limit=1);second=await self.console.list(limit=1,cursor=first['next_cursor'])
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        response=await self.client.post(f'/api/os/runtime/runs/{one["id"]}/cancel',headers=self.headers,
            json={'expected_version':0})
        self.assertEqual(response.status_code,409)
        response=await self.client.post(f'/api/os/runtime/runs/{one["id"]}/cancel',headers=self.headers,
            json={'expected_version':one['version']})
        self.assertEqual(response.json()['state'],'cancelled')

    async def test_preview_rejects_corrupted_content(self):
        r=await self.finish(await self.submit());a=r['artifacts'][0]
        self.console.provider.path(a['digest']).write_bytes(b'corrupted')
        with self.assertRaisesRegex(RuntimeErrorCode,'ARTIFACT_DIGEST_MISMATCH'):
            await self.console.preview(r['id'],a['id'])


class ReviewEvidenceTests(unittest.TestCase):
    def test_invalid_location_and_invented_quote_rejected(self):
        for start,quote in [(99,'items[0]'),(2,'invented code')]:
            with self.assertRaisesRegex(WorkloadError,'EVIDENCE_INSUFFICIENT'):
                review_result(sample('first-item'),{'summary':'issue','findings':[dict(title='issue',
                    line_start=start,line_end=start,condition='empty',evidence=quote,impact='exception')]})
