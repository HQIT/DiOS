"""Bounded Worker report outbox. Delivery ownership never grants execution authority."""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .database import RuntimeErrorCode
from .store import ACTIVE_ATTEMPTS, encoded, identifier


@dataclass(frozen=True)
class Receipt:
    event_id: str
    digest: str


@dataclass(frozen=True)
class Delivery:
    event_id: str
    digest: str
    payload: bytes
    token: str
    failures: int


class ReportSpool:
    """One local file per Worker installation; credentials and large artifacts stay outside.

    Limits count retained payloads, including blocked records. SQLite additionally bounds
    database pages; rollback journals require separate filesystem headroom. Synchronous
    methods are intended for a Worker thread, not an async service event loop.
    """
    def __init__(self, path, *, max_records=4096, max_bytes=8*1024*1024,
                 max_event_bytes=16384, clock=time.time):
        if any(type(v) is not int or v < 1 for v in (max_records, max_bytes, max_event_bytes)):
            raise RuntimeErrorCode('INVALID_SPOOL_LIMIT')
        self.path = Path(path).resolve()
        self.max_records, self.max_bytes = max_records, max_bytes
        self.max_event_bytes, self.clock = max_event_bytes, clock
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._transaction() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                c.execute('CREATE TABLE spool_meta (version INTEGER NOT NULL)')
                c.execute('INSERT INTO spool_meta VALUES (1)')
                c.execute('''CREATE TABLE reports (
                    event_id TEXT PRIMARY KEY, stream TEXT NOT NULL, sequence INTEGER NOT NULL,
                    digest TEXT NOT NULL, payload BLOB NOT NULL, created_at REAL NOT NULL,
                    next_at REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                    error TEXT, blocked INTEGER NOT NULL DEFAULT 0,
                    token TEXT, delivery_until REAL,
                    UNIQUE(stream, sequence))''')
            elif tables != {'spool_meta', 'reports'} or list(c.execute('SELECT version FROM spool_meta')) != [(1,)]:
                raise RuntimeErrorCode('SPOOL_SCHEMA_INCOMPATIBLE')

    @contextmanager
    def _transaction(self):
        c = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        try:
            c.execute('PRAGMA synchronous=FULL')
            # Payload quota plus bounded row/index overhead; no unbounded ACK history.
            page_size = c.execute('PRAGMA page_size').fetchone()[0]
            c.execute(f'PRAGMA max_page_count={math.ceil((self.max_bytes*2+self.max_records*2048+65536)/page_size)}')
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except sqlite3.OperationalError as exc:
            c.rollback()
            if exc.sqlite_errorcode == sqlite3.SQLITE_FULL:
                raise RuntimeErrorCode('SPOOL_FULL') from exc
            if exc.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                raise RuntimeErrorCode('SPOOL_BUSY') from exc
            raise
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def enqueue(self, scope, run_id, attempt_id, worker_id, instance, epoch, sequence, phase, **detail):
        for value in (scope, run_id, attempt_id, worker_id, instance): identifier(value)
        if type(epoch) is not int or epoch < 1 or type(sequence) is not int or sequence < 1 or phase not in ACTIVE_ATTEMPTS:
            raise RuntimeErrorCode('INVALID_REPORT')
        if set(detail) - {'session_id', 'process_id', 'process_instance', 'remote', 'output', 'output_observed_at'}:
            raise RuntimeErrorCode('INVALID_REPORT')
        if detail.get('output') and 'output_observed_at' not in detail:
            raise RuntimeErrorCode('OUTPUT_OBSERVATION_REQUIRED')
        payload = encoded(dict(scope=scope, run_id=run_id, attempt_id=attempt_id,
            worker_id=worker_id, instance=instance, epoch=epoch, sequence=sequence, phase=phase, **detail))
        if len(payload) > self.max_event_bytes: raise RuntimeErrorCode('SPOOL_EVENT_TOO_LARGE')
        stream = hashlib.sha256(encoded([scope, attempt_id])).hexdigest()
        event_id = hashlib.sha256(encoded([scope, attempt_id, sequence])).hexdigest()
        hashed = hashlib.sha256(payload).hexdigest()
        with self._transaction() as c:
            old = c.execute('SELECT digest FROM reports WHERE event_id=?', (event_id,)).fetchone()
            if old:
                if old[0] != hashed: raise RuntimeErrorCode('SPOOL_CONFLICT')
                return Receipt(event_id, hashed)
            count, size = c.execute('SELECT COUNT(*), COALESCE(SUM(length(payload)),0) FROM reports').fetchone()
            if count >= self.max_records or size+len(payload) > self.max_bytes:
                raise RuntimeErrorCode('SPOOL_FULL')
            now = self.clock()
            c.execute('''INSERT INTO reports (event_id,stream,sequence,digest,payload,created_at,next_at)
                VALUES (?,?,?,?,?,?,?)''', (event_id, stream, sequence, hashed, payload, now, now))
        return Receipt(event_id, hashed)

    def claim(self, lease_seconds=30):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise RuntimeErrorCode('INVALID_SPOOL_LIMIT')
        with self._transaction() as c:
            now = self.clock()
            row = c.execute('''SELECT r.event_id,r.digest,r.payload,r.failures FROM reports r
                WHERE r.blocked=0 AND r.next_at<=? AND (r.token IS NULL OR r.delivery_until<=?)
                AND NOT EXISTS (SELECT 1 FROM reports p WHERE p.stream=r.stream AND p.sequence<r.sequence)
                ORDER BY r.created_at,r.event_id LIMIT 1''', (now, now)).fetchone()
            if row is None: return None
            token = str(uuid4())
            c.execute('UPDATE reports SET token=?,delivery_until=? WHERE event_id=?', (token, now+lease_seconds, row[0]))
            return Delivery(row[0], row[1], row[2], token, row[3])

    def acknowledge(self, delivery, receipt):
        if not isinstance(receipt, Receipt) or (receipt.event_id, receipt.digest) != (delivery.event_id, delivery.digest):
            raise RuntimeErrorCode('SPOOL_RECEIPT_MISMATCH')
        with self._transaction() as c:
            return c.execute('DELETE FROM reports WHERE event_id=? AND digest=? AND token=?',
                (delivery.event_id, delivery.digest, delivery.token)).rowcount == 1

    def defer(self, delivery, code, *, blocked=False):
        identifier(code)
        with self._transaction() as c:
            delay = min(300, 2**min(delivery.failures, 9))
            return c.execute('''UPDATE reports SET failures=failures+1,error=?,blocked=?,next_at=?,
                token=NULL,delivery_until=NULL WHERE event_id=? AND token=?''',
                (code, int(blocked), self.clock()+delay, delivery.event_id, delivery.token)).rowcount == 1

    def status(self):
        with self._transaction() as c:
            return self._status(c, self.clock())

    @staticmethod
    def _status(c, now):
        count, size, blocked, inflight, oldest = c.execute('''SELECT COUNT(*),
            COALESCE(SUM(length(payload)),0),COALESCE(SUM(blocked),0),
            COALESCE(SUM(token IS NOT NULL AND delivery_until>?),0),MIN(created_at)
            FROM reports''', (now,)).fetchone()
        retry = c.execute('''SELECT MIN(MAX(next_at,COALESCE(delivery_until,0))) FROM reports r
            WHERE blocked=0 AND NOT EXISTS
            (SELECT 1 FROM reports p WHERE p.stream=r.stream AND p.sequence<r.sequence)''').fetchone()[0]
        errors = dict(c.execute('SELECT error,COUNT(*) FROM reports WHERE error IS NOT NULL GROUP BY error'))
        return dict(retained=count, payload_bytes=size, blocked=blocked, in_flight=inflight,
            oldest_at=oldest, next_delivery_at=retry, observed_at=now, errors=errors)

    @classmethod
    def inspect(cls, path):
        """Read-only operator query; never initialize a file or change its limits."""
        path = Path(path).resolve()
        if not path.is_file(): raise RuntimeErrorCode('SPOOL_NOT_FOUND')
        c = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1, isolation_level=None)
        try:
            c.execute('BEGIN')
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != {'spool_meta', 'reports'} or list(c.execute('SELECT version FROM spool_meta')) != [(1,)]:
                raise RuntimeErrorCode('SPOOL_SCHEMA_INCOMPATIBLE')
            return cls._status(c, time.time())
        finally:
            c.close()


async def accept_report(store, delivery):
    """Trusted in-process receiver seam; remote transport must authenticate the Worker.

    Uses the original Attempt/epoch and current server lease/authorization checks.
    A stale report remains reconciliation evidence; it cannot renew ownership.
    """
    if hashlib.sha256(delivery.payload).hexdigest() != delivery.digest:
        raise RuntimeErrorCode('SPOOL_DIGEST_MISMATCH')
    report = json.loads(delivery.payload.decode('utf-8'))
    if report.pop('scope') != store.scope: raise RuntimeErrorCode('WORKER_MISMATCH')
    expected = hashlib.sha256(encoded([store.scope, report['attempt_id'], report['sequence']])).hexdigest()
    if delivery.event_id != expected: raise RuntimeErrorCode('SPOOL_DIGEST_MISMATCH')
    await store.report(**report)
    return Receipt(delivery.event_id, delivery.digest)


async def flush_reports(spool, send, *, limit=100, timeout=10):
    """One bounded relay pass. Caller owns lifecycle, credentials and retry scheduling.

    Transport adapter maps network failures to ConnectionError and throttling to
    RATE_LIMITED. Unknown failures are retained as blocked evidence and re-raised.
    """
    if type(limit) is not int or not 1 <= limit <= 1000 or not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeErrorCode('INVALID_SPOOL_LIMIT')
    result = dict(acknowledged=0, deferred=0, blocked=0)
    transient = {'RATE_LIMITED', 'WRITE_QUEUE_FULL', 'WRITE_QUEUE_TIMEOUT', 'WRITE_DRAINING'}
    for _ in range(limit):
        delivery = await asyncio.to_thread(spool.claim, timeout+5)
        if delivery is None: break
        try:
            if hashlib.sha256(delivery.payload).hexdigest() != delivery.digest:
                raise RuntimeErrorCode('SPOOL_DIGEST_MISMATCH')
            receipt = await asyncio.wait_for(send(delivery), timeout)
            if await asyncio.to_thread(spool.acknowledge, delivery, receipt): result['acknowledged'] += 1
        except asyncio.CancelledError:
            # The send may have committed. Retrying the same report is idempotent.
            await asyncio.to_thread(spool.defer, delivery, 'DELIVERY_CANCELLED')
            raise
        except (ConnectionError, TimeoutError):
            await asyncio.to_thread(spool.defer, delivery, 'DELIVERY_UNAVAILABLE')
            result['deferred'] += 1
            break  # endpoint-wide outage: do not probe every queued record
        except RuntimeErrorCode as exc:
            blocked = exc.code not in transient
            await asyncio.to_thread(spool.defer, delivery, exc.code, blocked=blocked)
            result['blocked' if blocked else 'deferred'] += 1
            if not blocked: break
        except Exception:
            await asyncio.to_thread(spool.defer, delivery, 'DELIVERY_ADAPTER_ERROR', blocked=True)
            raise
    return result
