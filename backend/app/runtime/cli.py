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
from .history import HistoryQueries

async def execute(args):
    if args.command == 'spool-status':
        return await asyncio.to_thread(ReportSpool.inspect, args.file)
    if not args.database_url:
        raise RuntimeErrorCode('RUNTIME_DATABASE_URL_REQUIRED')
    db = Database(args.database_url)
    try:
        if args.command == 'migrate': return await db.migrate()
        if args.command == 'history':
            query = HistoryQueries(db, args.scope)
            if args.history_command == 'list':
                return await query.list_imported(source=args.source, kind=args.kind, limit=args.limit, cursor=args.cursor)
            if args.history_command == 'show':
                return await query.imported(args.source, args.kind, args.source_id, include_record=args.record)
            if args.history_command == 'links':
                return await query.related(args.source, args.kind, args.source_id,
                    direction=args.direction, limit=args.limit, cursor=args.cursor)
            if args.history_command == 'run':
                return await query.run(args.run_id, limit=args.limit,
                    attempt_cursor=args.attempt_cursor, artifact_cursor=args.artifact_cursor)
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
    history = commands.add_parser('history', help='query native execution and imported identity relationships')
    history_commands = history.add_subparsers(dest='history_command', required=True)
    history_list = history_commands.add_parser('list', help='page imported identities without original records')
    history_list.add_argument('--source');history_list.add_argument('--kind')
    history_show = history_commands.add_parser('show', help='inspect an imported identity; raw record is opt-in')
    history_show.add_argument('--record', action='store_true')
    history_links = history_commands.add_parser('links', help='page explicit source-adapter relationships')
    history_links.add_argument('--direction', choices=['outgoing','incoming'], default='outgoing')
    for command in (history_show, history_links):
        command.add_argument('source');command.add_argument('kind');command.add_argument('source_id')
    for command in (history_list, history_links):
        command.add_argument('--limit', type=int, default=50);command.add_argument('--cursor')
    history_run = history_commands.add_parser('run', help='read a native Run with paged attempts and artifact references')
    history_run.add_argument('run_id');history_run.add_argument('--limit', type=int, default=50)
    history_run.add_argument('--attempt-cursor');history_run.add_argument('--artifact-cursor')
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
