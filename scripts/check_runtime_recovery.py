"""Disposable recovery drill. No production database, credentials or Agent calls."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.runtime.database import Database,RuntimeErrorCode
from app.runtime.store import RuntimeStore,digest
from app.runtime.history import HistoryQueries
from app.runtime.artifacts import LocalArtifacts
from app.runtime.lifecycle import Lifecycle
from app.runtime.recovery import backup_sqlite,verify_backup,restore_sqlite


async def run():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);url='sqlite+aiosqlite:///'+str(root/'source.sqlite')
        db=Database(url);await db.migrate();store=RuntimeStore(db,'drill');life=Lifecycle(db,'drill')
        provider=LocalArtifacts(root/'objects');checks=[]
        try:
            await store.submit('one',{'fixture':'review'})
            await store.submit('two',{'fixture':'queued'})
            await store.register_worker('worker','instance','synthetic-host',os.getpid())
            a=await store.claim('worker','instance');run_id=a['run_id']
            binding=(run_id,a['id'],'worker','instance',a['epoch'])
            start=await life.begin_drain();assert start['active_attempts']==1
            await store.register_worker('new-worker','new-instance','synthetic-host',os.getpid())
            assert await store.claim('new-worker','new-instance') is None
            checks.append('new_worker_claim_blocked')
            for seq,phase in enumerate(['preparing','creating_session','waiting_model','validating'],1):
                await store.report(*binding,seq,phase,session_id='remote-ref' if seq>=3 else None)
            blob=provider.put(['中文验收报告\n'.encode()*1000])
            artifact=await store.complete(*binding,provider,blob.key,lambda chunks:len(b''.join(chunks))==blob.size)
            final=await life.wait_drained(1);assert final['ready_for_cutover'] and final['queued_runs']==1
            checks.append('active_attempt_completed_and_queued_run_retained')
            await store.import_history('legacy','task','old-task',{'state':'success','capabilities':None})
            await HistoryQueries(db,'drill').link('legacy','task','old-task','has_attempt','legacy','attempt','old-attempt')
            source_events=await store.pending_events('audit')
            bundle=root/'backup';target=root/'restored'
            manifest=await asyncio.to_thread(backup_sqlite,url,provider.root,bundle)
            await asyncio.to_thread(verify_backup,bundle)
            started=time.monotonic();receipt=await restore_sqlite(bundle,target)
            restored=Database('sqlite+aiosqlite:///'+str(target/'state.sqlite'))
            try:
                recovered=RuntimeStore(restored,'drill');query=HistoryQueries(restored,'drill')
                restored_events=await recovered.pending_events('audit')
                assert digest(restored_events)==digest(source_events)
                assert (await query.run(run_id))['artifacts']['items'][0]['id']==artifact
                assert (await query.related('legacy','task','old-task'))['items'][0]['resolution']=='unresolved'
                assert LocalArtifacts(target/'objects').verify(blob.key).size==blob.size
                checks.append('events_identity_history_links_and_artifact_bytes_verified')
                try:await recovered.claim('worker','instance')
                except RuntimeErrorCode as exc:assert exc.code=='RESTORE_RECONCILIATION_REQUIRED'
                else:raise AssertionError('restore must remain held')
                checks.append('restored_scheduling_held')
            finally:await restored.close()
            recovery_and_query_seconds=round(time.monotonic()-started,4)
            await life.resume(final['revision'])
            assert await store.claim('worker','instance') is not None
            checks.append('source_resume_uses_revision_and_existing_queue')
            return {'scope':'synthetic single-node SQLite recovery drill; not production RTO or real Agent lifecycle',
                'backup_seconds':manifest['duration_seconds'],'restore_seconds':receipt['duration_seconds'],
                'restore_and_query_seconds':recovery_and_query_seconds,
                'database_bytes':manifest['database']['size'],'artifact_bytes':blob.size,
                'counts':manifest['counts'],'event_digest':digest(source_events),'checks':checks,'successful':True,
                'limits':['SQLite/local artifacts only; PostgreSQL backup not implemented',
                    'External configuration, workspace files, Worker spools and Secret service backup remain separate',
                    'Restored scheduling release requires source isolation and task/effect reconciliation',
                    'Synthetic ledger lifecycle; actual Agent/process teardown and cross-host deployment pending']}
        finally:await db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args()
    report=asyncio.run(run())
    if args.output:
        path=args.output if args.output.is_absolute() else ROOT/args.output
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
