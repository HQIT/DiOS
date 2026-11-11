"""Bounded real-Runtime mixed-query smoke acceptance; no business execution."""
import argparse,asyncio,json,os,platform,random,sqlite3,statistics,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.runtime.database import Database
from app.runtime.store import RuntimeStore,digest

def p99(values):return round(sorted(values)[min(len(values)-1,int(.99*(len(values)-1)))],3)

async def sample(legacy,rep,seconds):
    with tempfile.TemporaryDirectory(prefix='dios-mixed-') as tmp:
        db=Database('sqlite+aiosqlite:///'+tmp+'/state.db')
        await db.migrate();store=RuntimeStore(db,'mixed')
        if legacy:db.write_gate=None  # Experiment-only reproduction of the old admission path.
        seeds=[await store.submit('seed-'+str(i),{'snapshot':str(i),'中文':'保留'}) for i in range(200)]
        writes=[];reads=[];errors=[];identities=[]
        begin=time.perf_counter()
        async def write(i):
            scheduled=begin+i/25
            await asyncio.sleep(max(0,scheduled-time.perf_counter()))
            try:
                value=await store.submit('mixed-'+str(i),{'snapshot':'sha:fixed','中文':'完整'})
                identities.append(value['id']);writes.append((time.perf_counter()-scheduled)*1000)
            except Exception as exc:errors.append(type(exc).__name__+':'+str(exc))
        async def read(i):
            scheduled=begin+i/10
            await asyncio.sleep(max(0,scheduled-time.perf_counter()))
            try:
                if i%2:
                    rows=await store.pending_events('dashboard',100)
                    assert all(digest(e['payload'])==e['digest'] for e in rows)
                else:
                    value=await store.get(seeds[i%len(seeds)]['id'])
                    assert value['input']['中文']=='保留' and value['version']==1
                reads.append((time.perf_counter()-scheduled)*1000)
            except Exception as exc:errors.append(type(exc).__name__+':'+str(exc))
        await asyncio.gather(*(write(i) for i in range(int(seconds*25))),*(read(i) for i in range(int(seconds*10))))
        ledger=await store.pending_events('verify',1000)
        assert not errors and len(identities)==seconds*25 and len(set(identities))==len(identities)
        assert len(ledger)==200+len(identities)
        assert all(digest(e['payload'])==e['digest'] for e in ledger)
        status=db.write_gate.snapshot() if db.write_gate else None
        await db.close()
        return dict(variant='legacy' if legacy else 'bounded_fifo',round=rep,duration_seconds=seconds,
            writes=len(writes),reads=len(reads),write_p99_ms=p99(writes),read_p99_ms=p99(reads),errors=errors,
            ledger_verified=True,queue=status,passed=p99(writes)<=1500 and p99(reads)<=1500)

async def main(args):
    cases=[(v,r) for r in range(3) for v in [True,False]];random.Random(3201).shuffle(cases)
    rows=[]
    for legacy,rep in cases:
        row=await sample(legacy,rep,5);rows.append(row);print(json.dumps(row),flush=True)
    report={'scope':'single-process actual Runtime SQLite mixed writes/queries; bounded smoke gate, not enterprise capacity',
        'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'journal':'existing driver default; no WAL upgrade in this change',
        'conditions':{'seed_runs':200,'writes_per_second':25,'queries_per_second':10,'rounds':3,'seconds_per_case':5,'queue_capacity':64,'queue_timeout_seconds':5},
        'thresholds':{'write_p99_ms':1500,'read_p99_ms':1500,'errors':0},'cases':rows,'passed':all(x['passed'] for x in rows if x['variant']=='bounded_fifo')}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert report['passed']

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);asyncio.run(main(p.parse_args()))
