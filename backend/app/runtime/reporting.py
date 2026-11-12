"""Worker-owned write-ahead phase reporting; retries preserve original identity."""
import asyncio
from hashlib import sha256
from sqlalchemy.exc import OperationalError, InterfaceError
from .spool import ReportSpool, accept_report, flush_reports


def report_spool(provider, scope):
    return ReportSpool(provider.root / ('.reports-' + sha256(scope.encode()).hexdigest() + '.sqlite'))


async def relay_reports(store, spool):
    async def send(delivery):
        try:
            return await accept_report(store, delivery)
        except (OperationalError, InterfaceError, OSError) as exc:
            raise ConnectionError('REPORT_STORAGE_UNAVAILABLE') from exc
    return await flush_reports(spool, send)


async def enqueue_report(store, spool, binding, sequence, phase, **details):
    receipt = await asyncio.to_thread(spool.enqueue, store.scope, *binding, sequence, phase, **details)
    await relay_reports(store, spool)
    return receipt
