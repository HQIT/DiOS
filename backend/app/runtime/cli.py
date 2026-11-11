"""Explicit local-operator administration. Never starts an Agent or legacy scheduler."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from sqlalchemy import select
from .database import Database, RuntimeErrorCode
from .store import RuntimeStore
from .schema import runs
from .spool import ReportSpool

async def execute(args):
    if args.command == 'spool-status':
        return await asyncio.to_thread(ReportSpool.inspect, args.file)
    if not args.database_url:
        raise RuntimeErrorCode('RUNTIME_DATABASE_URL_REQUIRED')
    db = Database(args.database_url)
    try:
        if args.command == 'migrate': return await db.migrate()
        store = RuntimeStore(db, args.scope)
        if args.command == 'submit':
            return await store.submit(args.key, json.loads(Path(args.file).read_text(encoding='utf-8')))
        if args.command == 'show': return await store.get(args.run_id)
        if args.command == 'events': return await store.pending_events(args.consumer, args.limit)
        if args.command == 'drain':
            await store.drain(args.worker_id)
            return {'worker_id': args.worker_id, 'draining': True}
        if args.command == 'list':
            async with db.engine.connect() as c:
                await db.check_schema(c)
                return [dict(x) for x in (await c.execute(select(runs.c.id,runs.c.state,runs.c.version,runs.c.updated_at)
                    .where(runs.c.scope==args.scope).order_by(runs.c.created_at.desc()).limit(args.limit))).mappings()]
    finally:
        await db.close()

def main(argv=None):
    p = argparse.ArgumentParser(description='DiOS durable runtime local administration')
    p.add_argument('--database-url',default=os.environ.get('DIOS_RUNTIME_DATABASE_URL'))
    p.add_argument('--scope',default='local')
    commands = p.add_subparsers(dest='command',required=True)
    commands.add_parser('migrate',help='explicit additive migration; backup existing database first')
    spool = commands.add_parser('spool-status', help='inspect retained Worker reports without payloads or network calls')
    spool.add_argument('--file', required=True)
    submit = commands.add_parser('submit',help='queue metadata only; does not launch an Agent')
    submit.add_argument('--key',required=True);submit.add_argument('--file',required=True)
    show = commands.add_parser('show');show.add_argument('run_id')
    listing = commands.add_parser('list');listing.add_argument('--limit',type=int,default=100)
    events = commands.add_parser('events');events.add_argument('--consumer',required=True);events.add_argument('--limit',type=int,default=100)
    drain = commands.add_parser('drain');drain.add_argument('worker_id')
    args = p.parse_args(argv)
    if hasattr(args,'limit') and not 1 <= args.limit <= 1000:p.error('limit must be 1..1000')
    try:
        value = asyncio.run(execute(args))
        print(json.dumps(value,ensure_ascii=False))
        return 0
    except RuntimeErrorCode as e:
        print(json.dumps({'error':e.code}))
        return 2
    except Exception as e:
        # Database exceptions can include credential URLs or input. Do not echo them.
        print(json.dumps({'error':'RUNTIME_COMMAND_FAILED','type':type(e).__name__}))
        return 1

if __name__ == '__main__': raise SystemExit(main())
