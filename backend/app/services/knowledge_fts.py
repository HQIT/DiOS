"""SQLite FTS5 adapter with a private, request-scoped authorized corpus."""
import asyncio
from collections import Counter
import sqlite3
import threading
import time

from app.runtime.database import RuntimeErrorCode
from .knowledge import ObjectKnowledgeProvider


def rank_fts(candidates, terms, stopped, *, timeout=10.0):
    connection = None
    deadline = time.monotonic() + timeout
    try:
        connection = sqlite3.connect(':memory:')
        connection.set_progress_handler(lambda: stopped.is_set() or time.monotonic() >= deadline, 1000)
        connection.execute('PRAGMA temp_store=MEMORY')
        # Terms and text share Python casefold; trigram preserves substring semantics.
        connection.execute("CREATE VIRTUAL TABLE corpus USING fts5(body, tokenize='trigram case_sensitive 1')")
        connection.executemany('INSERT INTO corpus(rowid,body) VALUES(?,?)',
                               [(i + 1, item[2].casefold()) for i, item in enumerate(candidates)])
        scores = Counter()
        for term in terms:
            if stopped.is_set() or time.monotonic() >= deadline:
                raise RuntimeErrorCode('KNOWLEDGE_QUERY_INTERRUPTED')
            if len(term) >= 3 and '\x00' not in term:
                expression = '"' + term.replace('"', '""') + '"'
                hits = connection.execute('SELECT rowid FROM corpus WHERE corpus MATCH ?', (expression,))
            else:
                # FTS5 cannot match fewer than 3 codepoints. Explicit short-term path.
                hits = connection.execute('SELECT rowid FROM corpus WHERE instr(body,?)>0', (term,))
            for (row_id,) in hits:
                scores[row_id] += 1
        matches = [(score, *candidates[row_id - 1]) for row_id, score in scores.items()]
        return sorted(matches, key=lambda hit: (-hit[0], hit[1]['id']))
    except sqlite3.Error as exc:
        code = 'KNOWLEDGE_QUERY_INTERRUPTED' if stopped.is_set() or time.monotonic() >= deadline else 'KNOWLEDGE_PROVIDER_UNAVAILABLE'
        raise RuntimeErrorCode(code) from exc
    finally:
        if connection is not None:
            connection.close()


class SQLiteFtsKnowledgeProvider(ObjectKnowledgeProvider):
    provider_id = 'sqlite-fts5-v1'
    ranking = 'fts5-trigram-literal-terms-v1'

    def capabilities(self):
        return dict(super().capabilities(), index='request-scoped-fts5-trigram',
                    index_lifetime='single-query', short_terms='sqlite-instr',
                    engine_version=sqlite3.sqlite_version, rank_timeout_seconds=10)

    async def rank(self, candidates, terms):
        stopped = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(rank_fts, candidates, terms, stopped))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stopped.set()
            try:
                await task
            except RuntimeErrorCode:
                pass
            raise
