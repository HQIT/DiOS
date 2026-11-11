"""Aggregate repeated runs without pooling per-run percentiles."""
import argparse,collections,hashlib,json,statistics
from pathlib import Path

def summarize(paths):
    groups=collections.defaultdict(list);inputs=[]
    for path in paths:
        data=path.read_bytes();inputs.append({'path':str(path),'sha256':hashlib.sha256(data).hexdigest()})
        for line in data.decode().splitlines():
            r=json.loads(line)
            key=(r.get('confirmation',False),r['kind'],r['workload'],r['concurrency'],r['arrival'],r['offered_rate'])
            groups[key].append(r)
    rows=[]
    def stats(values):return {'median':statistics.median(values),'min':min(values),'max':max(values)}
    for key,values in groups.items():
        confirm,kind,workload,concurrency,arrival,rate=key
        assert len(values)==3 and {x['round'] for x in values}=={0,1,2},key
        rows.append({'confirmation':confirm,'kind':kind,'workload':workload,'concurrency':concurrency,'arrival':arrival,'offered_rate':rate,
            'rounds':3,'throughput':stats([x['throughput'] for x in values]),
            'p99_ms':stats([x['latency_ms']['p99'] for x in values]),'p50_ms':stats([x['latency_ms']['p50'] for x in values]),
            'cpu_seconds_per_1000_commits':stats([x['db_cpu_seconds']*1000/x['completed_total'] for x in values]),
            'memory_end_mib':stats([x['db_memory_end_bytes']/1024**2 for x in values]),
            'retries_per_commit':stats([x['retries_total']/x['completed_total'] for x in values]),
            'dropped':sum(x['dropped'] for x in values),'arrivals':sum(x['arrivals'] for x in values),
            'errors':sum(x['error_count'] for x in values),'verified':all(x['verified'] for x in values)})
    return {'scope':'single-node intent commit; repeated-round medians and min/max, not confidence intervals',
            'inputs':inputs,'cases':sum(len(x) for x in groups.values()),'groups':rows,
            'errors':sum(r['errors'] for r in rows),'verified':all(r['verified'] for r in rows)}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('inputs',nargs='+',type=Path);p.add_argument('--output',required=True,type=Path);args=p.parse_args()
    value=summarize(args.inputs);args.output.write_text(json.dumps(value,indent=2)+'\n');print(json.dumps({'cases':value['cases'],'errors':value['errors'],'verified':value['verified']}))
