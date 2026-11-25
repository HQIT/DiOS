"""Real object reads under source replacement, ACL changes and restore."""
import asyncio
import time
from unittest.mock import patch
import unittest

from app.runtime.database import Database
from app.runtime.recovery import backup_sqlite, restore_sqlite
from app.services.knowledge import ObjectKnowledgeProvider
from app.services.knowledge_providers import knowledge_provider
from app.services.task_console import TaskConsole
import test_runtime_object_access as fixtures


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ObjectAccessTests.asyncSetUp
    asyncTearDown = fixtures.ObjectAccessTests.asyncTearDown
    save = fixtures.ObjectAccessTests.save
    headers_for = fixtures.ObjectAccessTests.headers_for
    publish = fixtures.ObjectAccessTests.publish
    set_policy = fixtures.ObjectAccessTests.set_policy
    path = fixtures.ObjectAccessTests.path

    async def change(self, key, ref=None, revision=0, **extra):
        body = {'expected_revision': revision, 'idempotency_key': key + ':' + str(revision),
                'state': 'active' if ref else 'deleted', 'object_ref': ref,
                'provenance': {'namespace': 'source', 'key': key, 'version': 'opaque-v1', 'locator': 'record/1'} if ref else None,
                **extra}
        return await self.client.put('/api/os/runtime/knowledge/sources/' + key, headers=self.headers, json=body)

    async def search(self, who='a', query='共同', **extra):
        return await self.client.post('/api/os/runtime/knowledge/search', headers=self.headers_for(who),
                                      json={'query': query, **extra})

    async def seeded(self):
        ref = await self.publish('source', '共同 中文🙂来源'.encode())
        await self.set_policy(ref, ['a'], key='a')
        response = await self.change('source', ref)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['operation_status'], 'applied')
        detail = await self.client.get('/api/os/runtime/knowledge/sources/source', headers=self.headers)
        self.assertEqual(detail.json()['object_ref'], ref)
        denied = await self.client.get('/api/os/runtime/knowledge/sources/source', headers=self.headers_for('a'))
        self.assertEqual(denied.status_code, 403)
        return ref

    async def test_two_subjects_same_query_exact_citations_and_no_authority_in_arguments(self):
        ref = await self.seeded()
        response = await self.search()
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()['items'][0]
        self.assertEqual(item['object_ref'], ref)
        raw = await self.client.get(self.path(ref) + '/download', headers=self.headers_for('a'))
        self.assertEqual(item['text'].encode(), raw.content)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual((await self.search('b')).json()['items'], [])
        self.assertEqual((await self.search(project_id='forged')).status_code, 422)
        self.assertEqual((await self.search(actor='forged')).status_code, 422)
        denied = await self.client.put('/api/os/runtime/knowledge/sources/source', headers=self.headers_for('a'),
                                       json={'expected_revision': 1, 'idempotency_key': 'self', 'state': 'deleted'})
        self.assertEqual(denied.status_code, 403)

    async def test_replace_delete_replay_and_revoke_leave_no_stale_content(self):
        ref = await self.seeded()
        self.assertEqual(len((await self.search()).json()['items']), 1)
        latest = await self.publish('v2', '共同 新版本'.encode(), object_id=ref['id'], expected_version=1)
        changed = await self.change('source', latest, 1)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((await self.search()).json()['items'], [])
        await self.set_policy(latest, ['b'], key='b')
        item = (await self.search('b')).json()['items'][0]
        self.assertEqual((item['object_ref'], item['source_revision']), (latest, 2))
        self.assertEqual((await self.change('source', revision=2)).status_code, 200)
        replay = await self.change('source', latest, 1)
        self.assertEqual(replay.json(), changed.json())
        self.assertEqual((await self.search('b')).json()['items'], [])
        await self.change('source', latest, 3)
        await self.set_policy(latest, [], revision=1, key='revoke')
        self.assertEqual((await self.search('b')).json()['items'], [])

    async def test_source_and_object_expiry_deletion(self):
        ref = await self.seeded()
        expiry = time.time() + 2
        response = await self.change('source', ref, 1, expires_at=expiry)
        self.assertEqual(response.status_code, 200, response.text)
        await asyncio.sleep(max(0, expiry - time.time()) + .1)
        self.assertEqual((await self.search()).json()['items'], [])
        await self.change('source', ref, 2)
        response = await self.client.post(self.path(ref) + '/delete', headers=self.headers,
            json={'expected_revision': 0, 'idempotency_key': 'delete-source-object', 'reason': 'test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.search()).json()['items'], [])

    async def test_mutation_during_storage_read_invalidates_response(self):
        await self.seeded()
        original = ObjectKnowledgeProvider.content
        async def changed(provider, ref):
            value = await original(provider, ref)
            response = await self.change('source', revision=1)
            self.assertEqual(response.status_code, 200)
            return value
        with patch.object(ObjectKnowledgeProvider, 'content', changed):
            response = await self.search()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail'], 'KNOWLEDGE_VIEW_CHANGED')

    async def test_revocation_during_storage_read_filters_already_buffered_text(self):
        ref = await self.seeded()
        original = ObjectKnowledgeProvider.content
        async def revoked(provider, reference):
            value = await original(provider, reference)
            response = await self.set_policy(ref, [], revision=1, key='in-flight-revoke')
            self.assertEqual(response.status_code, 200)
            return value
        with patch.object(ObjectKnowledgeProvider, 'content', revoked):
            response = await self.search()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['items'], [])

    async def test_concurrent_replacement_cas_and_restore_keep_source_history(self):
        ref = await self.seeded()
        responses = await asyncio.gather(self.change('source', ref, 1, idempotency_key='race1'),
                                          self.change('source', ref, 1, idempotency_key='race2'))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        await asyncio.to_thread(backup_sqlite, self.db.engine.url.render_as_string(hide_password=False),
                                self.console.provider.root, self.root / 'backup')
        await restore_sqlite(self.root / 'backup', self.root / 'restored')
        db = Database('sqlite+aiosqlite:///' + str(self.root / 'restored/state.sqlite'))
        console = TaskConsole(db, 'one', self.profiles, self.root / 'restored/objects', policy=self.console.policy)
        try:
            result = await knowledge_provider(console).search('共同')
            self.assertEqual(result['items'][0]['object_ref'], ref)
            self.assertEqual(result['items'][0]['source_revision'], 2)
        finally:
            console.provider.close()
            await db.close()
