"""Longer independent confirmation for three finalists: optimized PG and both embedded candidates."""
import argparse,asyncio,json,os,random
from pathlib import Path
from .run import benchmark,docker,NAMES

def reset(kind):
    name=NAMES[kind]
    assert docker("inspect","--format",'{{index .Config.Labels "dios.purpose"}}',name)=="provider-evaluation-32"
    if kind=="pg-row":
        docker("exec",name,"psql","-U","postgres","-d","p32","-c","TRUNCATE p32_entries,p32_states")
    elif kind=="sqlite-wal":
        docker("exec",name,"python","-c",'import apsw;c=apsw.Connection("/data/store.sqlite");c.execute("DELETE FROM entries");c.execute("DELETE FROM states");c.execute("PRAGMA wal_checkpoint(TRUNCATE)");c.execute("VACUUM")')
    else:
        docker("exec",name,"python","-c",'import lmdb;e=lmdb.open("/data",map_size=2*1024**3,max_dbs=2);a=e.open_db(b"states");b=e.open_db(b"entries");t=e.begin(write=True);t.drop(a,delete=False);t.drop(b,delete=False);t.commit();e.close()')


async def main(args):
    os.sched_setaffinity(0,set(sorted(os.sched_getaffinity(0))[:2]))
    screen=[json.loads(x) for x in Path(args.screen).read_text().splitlines()]
    assert len(screen)==132 and all(x['verified'] and not x['error_count'] for x in screen)
    # Open arrivals below and near the embedded write ceiling, using the same clock/queue.
    extra=Path(args.output).with_name('provider-lightweight-arrival-20261111.jsonl')
    with extra.open('w') as out:
        for rep in range(3):
            batch=[(k,rate) for k in ['sqlite-wal','lmdb-txn'] for rate in [25,100]]
            random.Random(56000+rep).shuffle(batch)
            for kind,rate in batch:
                row=await benchmark(kind,32,'uniform',rep,5,rate);row['extension']=True
                out.write(json.dumps(row)+'\n');out.flush()
                print(f"arrival {rep+1}/3 {kind} rate={rate} tps={row['throughput']} p99={row['latency_ms'].get('p99')} dropped={row['dropped']}",flush=True)
    cases=[]
    for rep in range(3):
        batch=[(k,w,rep) for k in ['pg-row','sqlite-wal','lmdb-txn'] for w in ['uniform','hotspot']]
        random.Random(64000+rep).shuffle(batch);cases.extend(batch)
    with Path(args.output).open('w') as out:
        for index,(kind,workload,rep) in enumerate(cases):
            reset(kind)
            row=await benchmark(kind,32,workload,rep,15)
            row["data_baseline"]="empty logical tables before warmup; physical engine files/cache retained"
            row['confirmation']=True;row['order']=index
            out.write(json.dumps(row)+'\n');out.flush()
            print(f"confirm {index+1}/18 {kind} {workload} tps={row['throughput']} p99={row['latency_ms'].get('p99')} errors={row['error_count']}",flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--screen',required=True);p.add_argument('--output',required=True)
    asyncio.run(main(p.parse_args()))
