"""Reproducible single-node intent-commit comparison. No business execution."""
import argparse,asyncio,json,os,random,resource,statistics,subprocess,time,uuid
from pathlib import Path
import httpx
from .model import action,verify,hashed
from .providers import connect
KINDS=['pg-workspace','pg-row','mysql-row','etcd-cas']
EXTRA_KINDS=['sqlite-wal','lmdb-txn']
NAMES={'pg-workspace':'dios-p32-pg','pg-row':'dios-p32-pg','mysql-row':'dios-p32-mysql','etcd-cas':'dios-p32-etcd','sqlite-wal':'dios-p32-sqlite','lmdb-txn':'dios-p32-lmdb'}

def quantile(values,p):
    if not values:return None
    x=sorted(values);return x[min(len(x)-1,int((len(x)-1)*p))]
def summarize(values):return {k:round(quantile(values,p),4) for k,p in [('p50',.5),('p95',.95),('p99',.99)]} if values else {}
def stats(kind):
    with httpx.Client(transport=httpx.HTTPTransport(uds='/var/run/docker.sock'),base_url='http://docker',trust_env=False) as client:
        value=client.get('/v1.47/containers/'+NAMES[kind]+'/stats?stream=false&one-shot=true').json()
    io=value.get('blkio_stats',{}).get('io_service_bytes_recursive') or []
    return {'cpu_seconds':value['cpu_stats']['cpu_usage']['total_usage']/1e9,
        'memory_bytes':value.get('memory_stats',{}).get('usage'),
        'write_bytes':sum(v['value'] for v in io if v['op'].lower()=='write')}
def docker(*args):
    return subprocess.check_output(['docker',*args],text=True,stderr=subprocess.STDOUT).strip()
async def checked_store(kind,scope):
    for n in range(60):
        try:return await connect(kind,scope)
        except Exception:
            if n==59:raise
            await asyncio.sleep(.5)

async def correctness(kind):
    scope='check-'+uuid.uuid4().hex[:12];s=await checked_store(kind,scope);checks=[]
    async def check(name,condition):
        assert condition,name
        checks.append(name)
    try:
        await s.create('replay')
        q=action('replay','same')
        result=await asyncio.gather(*(s.apply('replay',q) for _ in range(32)))
        await check('duplicate_trigger_one_commit',sum(x['status']=='committed' for x in result)==1)
        await check('idempotency_payload_conflict',(await s.apply('replay',action('replay','same',parameters={'path':'changed'})))['status']=='conflict')
        await s.create('approval')
        result=await asyncio.gather(*(s.apply('approval',action('approval',str(i),approval=True)) for i in range(32)))
        await check('approval_single_consumption',sum(x['status']=='committed' for x in result)==1)
        await s.create('revocation')
        jobs=[s.apply('revocation',action('revocation',str(i))) for i in range(32)]
        jobs.insert(16,s.apply('revocation',{'run_id':'revocation','id':'revoke','kind':'revoke'}))
        await asyncio.gather(*jobs)
        await check('post_revoke_denied',(await s.apply('revocation',action('revocation','after')))['status']=='deny')
        await s.create('fencing');await s.apply('fencing',{'run_id':'fencing','id':'new-owner','kind':'epoch'})
        await check('old_epoch_denied',(await s.apply('fencing',action('fencing','old')))['status']=='deny')
        await check('current_epoch_allowed',(await s.apply('fencing',action('fencing','new',epoch=2)))['status']=='committed')
        await check('out_of_scope_denied',(await s.apply('fencing',action('fencing','scope',epoch=2,resource='snapshot:other')))['status']=='deny')
        await s.create('failure');s.fail_before_commit=True
        try:await s.apply('failure',action('failure','failed'))
        except OSError:pass
        else:raise AssertionError('injected failure did not fail')
        s.fail_before_commit=False
        await check('write_failure_no_partial_state',(await s.state('failure'))['version']==0 and len(await s.entries('failure'))==0)
        await s.apply('failure',action('failure','after-recovery'))
        # Client loses a successful response: repeat the identity, never fabricate success.
        lost=action('failure','response-lost');await s.apply('failure',lost)
        await check('response_loss_reconciliation',(await s.apply('failure',lost))['status']=='replay')
        for run in ['replay','approval','revocation','fencing','failure']:
            verify(await s.state(run),await s.entries(run))
        await check('independent_evidence_validation',True)
        # Real process kill of this isolated database, followed by recovery of every acked entry.
        label=docker('inspect','--format','{{index .Config.Labels "dios.purpose"}}',NAMES[kind])
        assert label=='provider-evaluation-32'
        expected={run:{'state':await s.state(run),'entries':sorted(await s.entries(run),key=lambda x:x['version'])} for run in ['replay','approval','revocation','fencing','failure']}
        await s.close();before=time.perf_counter();docker('kill','--signal=KILL',NAMES[kind]);docker('start',NAMES[kind])
        s=await checked_store(kind,scope)
        for run in ['replay','approval','revocation','fencing','failure']:verify(await s.state(run),await s.entries(run))
        recovered={run:{'state':await s.state(run),'entries':sorted(await s.entries(run),key=lambda x:x['version'])} for run in expected}
        await check('acknowledged_state_survives_kill',recovered==expected)
        recovery=time.perf_counter()-before
        # Real server pause plus client timeout: unresolved commit is reconciled by identity.
        paused=action('failure','pause-retry');docker('pause',NAMES[kind])
        async def release_pause():
            await asyncio.sleep(.6)
            docker('unpause',NAMES[kind])
        release=asyncio.create_task(release_pause())
        try:
            try:await asyncio.wait_for(s.apply('failure',paused),.15)
            except asyncio.TimeoutError:pass
            else:raise AssertionError('paused server unexpectedly answered')
        finally:await release
        await s.close();s=await checked_store(kind,scope)
        result=await s.apply('failure',paused)
        await check('timeout_reconciles_once',result['status'] in {'replay','committed'} and len(await s.entries('failure'))==3)
        verify(await s.state('failure'),await s.entries('failure'))
        return {'kind':kind,'checks':checks,'passed':len(checks),'recovery_seconds':round(recovery,3)}
    finally:await s.close()

async def benchmark(kind,concurrency,workload,round_id,duration,rate=None):
    scope='b-'+uuid.uuid4().hex[:14];s=await connect(kind,scope)
    run_ids=[f'r{i}' for i in range(64 if workload=='uniform' else 1)]
    try:
        for rid in run_ids:await s.create(rid)
        if workload=='long_history':
            for i in range(1024):await s.apply('r0',action('r0',f'history-{i}'))
        # Exercise the same concurrency and all pool slots before timing.
        warm_deadline=time.perf_counter()+2
        async def warm(worker):
            index=worker
            while time.perf_counter()<warm_deadline:
                rid=run_ids[index%len(run_ids)]
                await s.apply(rid,action(rid,f'warm-{index}'))
                index+=concurrency
        await asyncio.gather(*(warm(i) for i in range(concurrency)))
        latencies=[];services=[];rows=[];timing={};errors=[];committed=arrivals=dropped=0
        before=stats(kind);client_cpu=time.process_time();start=time.perf_counter();deadline=start+duration
        queue=asyncio.Queue(maxsize=256)
        async def one(i,scheduled):
            nonlocal committed
            rid=run_ids[i%len(run_ids)];entered=time.perf_counter()
            try:
                result=await s.apply(rid,action(rid,f'measure-{i}',parameters={'path':'src/example.py','context':'x'*128}))
                done=time.perf_counter();latencies.append((done-scheduled)*1000);services.append((done-entered)*1000)
                if result['status']=='committed':
                    if done<=deadline:committed+=1
                else:errors.append(result['status'])
                for k,v in result['timing'].items():timing.setdefault(k,[]).append(v)
            except Exception as e:errors.append(type(e).__name__)
        async def closed(worker):
            nonlocal arrivals
            i=worker
            while time.perf_counter()<deadline:
                arrivals+=1;await one(i,time.perf_counter());i+=concurrency
        async def consumer():
            while True:
                item=await queue.get()
                try:
                    if item is None:return
                    await one(*item)
                finally:queue.task_done()
        if rate is None:await asyncio.gather(*(closed(i) for i in range(concurrency)))
        else:
            tasks=[asyncio.create_task(consumer()) for _ in range(concurrency)]
            for i in range(int(duration*rate)):
                scheduled=start+i/rate
                await asyncio.sleep(max(0,scheduled-time.perf_counter()));arrivals+=1
                try:queue.put_nowait((i,scheduled))
                except asyncio.QueueFull:dropped+=1
            await queue.join()
            for _ in tasks:await queue.put(None)
            await asyncio.gather(*tasks)
        end=time.perf_counter();cpu=time.process_time()-client_cpu;after=stats(kind)
        evidence=[]
        for rid in run_ids:
            state=await s.state(rid);entries=await s.entries(rid);verify(state,entries)
            evidence.append({'run_id':rid,'version':state['version'],'head':state['head']})
        return {'kind':kind,'concurrency':concurrency,'workload':workload,'round':round_id,'arrival':'closed' if rate is None else 'open','offered_rate':rate,
            'duration_seconds':duration,'drain_seconds':round(max(0,end-deadline),4),'arrivals':arrivals,'dropped':dropped,
            'completed_in_window':committed,'completed_total':len(latencies),'throughput':round(committed/duration,3),
            'latency_ms':summarize(latencies),'service_ms':summarize(services),'stages':{k:summarize(v) for k,v in timing.items()},
            'retries_total':sum(timing.get('retries',[])),'errors':errors[:10],'error_count':len(errors),
            'db_cpu_seconds':round(after['cpu_seconds']-before['cpu_seconds'],4),'client_cpu_seconds':round(cpu,4),
            'db_memory_end_bytes':after['memory_bytes'],'db_write_bytes':after['write_bytes']-before['write_bytes'],
            'evidence_root':hashed(evidence),'evidence_records':sum(x['version'] for x in evidence),'verified':True,
            'latency_sample_ms':[round(x,3) for x in latencies[::max(1,len(latencies)//200)]]}
    finally:await s.close()

async def main(args):
    allowed=sorted(os.sched_getaffinity(0));os.sched_setaffinity(0,set(allowed[:2]))
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    if args.mode=='check':
        result=json.loads(out.read_text()) if out.exists() else []
        result=[x for x in result if x['kind'] not in args.kinds]
        for kind in args.kinds:
            value=await correctness(kind);result.append(value);out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(value),flush=True)
        out.write_text(json.dumps(result,indent=2)+'\n');return
    cases=[]
    for rep in range(args.rounds):
        batch=[(k,c,w,rep,None) for k in args.kinds for c in [1,8,32,128] for w in ['uniform','hotspot']]
        batch += [(k,8,'long_history',rep,None) for k in args.kinds]
        batch += [(k,32,'uniform',rep,r) for k in args.kinds for r in [100,500]]
        random.Random(32000+rep).shuffle(batch);cases.extend(batch)
    with out.open('w',encoding='utf-8') as f:
        for index,(k,c,w,rep,rate) in enumerate(cases):
            result=await benchmark(k,c,w,rep,args.seconds,rate)
            result['order']=index;f.write(json.dumps(result)+'\n');f.flush()
            print(f"{index+1}/{len(cases)} {k} {w} c={c} rate={rate} tps={result['throughput']} p99={result['latency_ms'].get('p99')} errors={result['error_count']}",flush=True)
    print('Completed controlled comparison',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['check','benchmark'],default='check');p.add_argument('--output',required=True);p.add_argument('--rounds',type=int,default=3);p.add_argument('--seconds',type=float,default=5);p.add_argument('--kinds',nargs='+',choices=KINDS+EXTRA_KINDS,default=KINDS)
    asyncio.run(main(p.parse_args()))
