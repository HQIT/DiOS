"""Matched core screening of the embedded candidates."""
import argparse,asyncio,json,os,random
from pathlib import Path
from .run import benchmark
async def main(args):
    os.sched_setaffinity(0,set(sorted(os.sched_getaffinity(0))[:2]))
    cases=[]
    for rep in range(3):
        batch=[(k,c,w,rep,None) for k in ['sqlite-wal','lmdb-txn'] for c in [1,32] for w in ['uniform','hotspot']]
        batch += [(k,8,'long_history',rep,None) for k in ['sqlite-wal','lmdb-txn']]
        batch += [(k,32,'uniform',rep,500) for k in ['sqlite-wal','lmdb-txn']]
        random.Random(48000+rep).shuffle(batch);cases.extend(batch)
    with Path(args.output).open('w') as out:
        for index,(k,c,w,rep,rate) in enumerate(cases):
            row=await benchmark(k,c,w,rep,5,rate);row['order']=index;row['extension']=True
            out.write(json.dumps(row)+'\n');out.flush()
            print(f"light {index+1}/36 {k} {w} c={c} rate={rate} tps={row['throughput']} p99={row['latency_ms'].get('p99')} errors={row['error_count']}",flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);asyncio.run(main(p.parse_args()))
