"""The same API/identity/lifecycle fixture against the real SQLite FTS5 adapter."""
import asyncio
import sqlite3
import threading
import unittest
from unittest.mock import patch

from app.config import settings
from app.runtime.database import RuntimeErrorCode
from app.services.knowledge_fts import SQLiteFtsKnowledgeProvider, rank_fts
import test_runtime_knowledge as shared


class FtsKnowledgeTests(shared.KnowledgeTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        selected = patch.object(settings, 'runtime_knowledge_provider', 'sqlite-fts5-v1')
        selected.start()
        self.addCleanup(selected.stop)

    async def test_actual_fts_with_same_principal_and_query_across_provider_switch(self):
        ref = await self.publish('fts', '知识契约测试 Straße alpha "quote" OR %_ 🙂'.encode())
        await self.set_policy(ref, ['a'], key='fts-grant')
        await self.change('fts', ref)
        for query in ('知识契约', '共同', 'alpha', 'STRASSE', '"quote"', 'OR', '%_', '🙂'):
            with patch.object(settings, 'runtime_knowledge_provider', 'object-read-through-v1'):
                direct = (await self.search(query=query)).json()
            indexed = (await self.search(query=query)).json()
            self.assertEqual(indexed['provider'], 'sqlite-fts5-v1')
            self.assertEqual(indexed['items'], direct['items'], query)
            self.assertEqual((await self.search('b', query=query)).json()['items'], [])
        caps = (await self.client.get('/api/os/runtime/knowledge/capabilities', headers=self.headers)).json()
        self.assertEqual(caps['engine_version'], sqlite3.sqlite_version)
        self.assertEqual(caps['index_lifetime'], 'single-query')
        self.assertEqual((await self.search(provider='object-read-through-v1')).status_code, 422)

    async def test_provider_failure_does_not_silently_fall_back_or_return_empty_success(self):
        await self.seeded()
        with patch.object(settings, 'runtime_knowledge_provider', 'missing'):
            response = await self.search()
            self.assertEqual(response.status_code, 503)
        with patch('app.services.knowledge_fts.sqlite3.connect', side_effect=sqlite3.OperationalError('unavailable')):
            response = await self.search()
            self.assertEqual(response.status_code, 503)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_match_query_literal_escaping_and_connection_closed(self):
        statements, closed = [], threading.Event()
        class Traced(sqlite3.Connection):
            def close(self):
                super().close()
                closed.set()
        connect = sqlite3.connect
        def traced(path):
            self.assertEqual(path, ':memory:')
            connection = connect(path, factory=Traced)
            connection.set_trace_callback(statements.append)
            return connection
        corpus = [({'id':'one'}, {}, 'alpha "literal" 中文知识库'), ({'id':'two'}, {}, 'different')]
        with patch('app.services.knowledge_fts.sqlite3.connect', traced):
            result = await asyncio.to_thread(rank_fts, corpus, ['alpha', '"literal"', '中文知'], threading.Event())
        self.assertEqual([(hit[0], hit[1]['id']) for hit in result], [(3, 'one')])
        self.assertTrue(any('corpus MATCH' in sql for sql in statements))
        self.assertTrue(closed.is_set())

    async def test_cancellation_waits_for_index_worker_to_stop(self):
        entered, ended = threading.Event(), threading.Event()
        def rank(candidates, terms, stopped):
            entered.set()
            if not stopped.wait(3):
                raise AssertionError('Cancellation was not forwarded')
            ended.set()
            raise RuntimeErrorCode('KNOWLEDGE_QUERY_INTERRUPTED')
        provider = object.__new__(SQLiteFtsKnowledgeProvider)
        with patch('app.services.knowledge_fts.rank_fts', rank):
            task = asyncio.create_task(provider.rank([], []))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(ended.is_set())

    async def test_elapsed_budget_interrupts_actual_engine(self):
        with self.assertRaisesRegex(RuntimeErrorCode, 'KNOWLEDGE_QUERY_INTERRUPTED'):
            rank_fts([({'id':'one'}, {}, 'alpha')], ['alpha'], threading.Event(), timeout=0)
