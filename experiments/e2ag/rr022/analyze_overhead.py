"""Read-only numerical analysis of paired timing runs and completed span trees."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics
from performance_pretrial import percentile
from overhead_spans import exclusive_rows


def distribution(values):
    return {'n': len(values), 'median': statistics.median(values) if values else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}


def paired(rows, instrument=False):
    groups = defaultdict(dict)
    for r in rows:
        key = (r['kind'], r['concurrency'], r['repeat'])
        arm = r['instrument'] if instrument else r['mode']
        if arm in groups[key]:
            raise ValueError('duplicate pair member')
        groups[key][arm] = r
    result = []
    for key, arms in groups.items():
        left, right = (False, True) if instrument else ('G0', 'G2')
        if set(arms) != {left, right}:
            raise ValueError('incomplete pair')
        a, b = arms[left]['summary'], arms[right]['summary']
        p50a, p50b = a['workflow']['p50_ms'], b['workflow']['p50_ms']
        result.append({'kind': key[0], 'concurrency': key[1], 'repeat': key[2],
                       'left_p50_ms': p50a, 'right_p50_ms': p50b,
                       'delta_p50_ms': p50b-p50a, 'delta_p50_pct': 100*(p50b-p50a)/p50a,
                       'left_errors': a['errors'], 'right_errors': b['errors'],
                       'left_n': a['workflow']['n'], 'right_n': b['workflow']['n']})
    return result


def proxy_intervals(spans):
    """Same-process route intervals around one upstream exchange, in milliseconds."""
    requests = defaultdict(list)
    for row in spans:
        if row['request_id'].startswith('measurement-'):
            requests[row['request_id']].append(row)
    result = []
    for rid, rows in requests.items():
        if not rid.endswith((':read', ':write')):
            continue
        roots = [s for s in rows if s['stage'] == 'http_route']
        upstream = [s for s in rows if s['stage'] == 'upstream_roundtrip']
        if len(roots) != 1 or len(upstream) != 1:
            raise ValueError('expected one route and one upstream exchange: ' + rid)
        root, tool = roots[0], upstream[0]
        if not root['start_ns'] <= tool['start_ns'] <= tool['end_ns'] <= root['end_ns']:
            raise ValueError('upstream outside route')
        result.append({'request_id': rid, 'tool': rid.rsplit(':', 1)[1],
                       'pre_upstream_ms': (tool['start_ns']-root['start_ns'])/1e6,
                       'upstream_ms': (tool['end_ns']-tool['start_ns'])/1e6,
                       'post_upstream_ms': (root['end_ns']-tool['end_ns'])/1e6})
    return result


def analyze(folder):
    data = json.loads((folder/'results.json').read_text())
    manifest = json.loads((folder/'manifest.json').read_text())
    if not data['finished'] or not data['source_unchanged']:
        raise ValueError('suite incomplete or source changed')
    if not all(r['qualification_expected'] for r in data['rows']):
        raise ValueError('measurement validation failed')
    profile = manifest['profile']
    result = {'profile': profile, 'cells': len(data['rows']), 'valid': True}
    if profile != 'qualification':
        pairs = paired(data['rows'], instrument=profile=='instrumentation')
        result['pairs'] = pairs
        for kind in ['issue', 'pr']:
            for n in sorted({r['concurrency'] for r in pairs}):
                group = [r for r in pairs if r['kind']==kind and r['concurrency']==n]
                result[f'{kind}_c{n}'] = {'delta_p50_ms': distribution([r['delta_p50_ms'] for r in group]),
                                         'delta_p50_pct': distribution([r['delta_p50_pct'] for r in group])}
    stage_rows = []
    cell_rows = []
    for index, r in enumerate(data['rows']):
        # Relocate remote artifacts by cell directory name, without relying on /work.
        cell = folder / Path(r['directory']).name
        spans = exclusive_rows(json.loads((cell/'server-spans.json').read_text()))
        raw = json.loads((cell/'client-raw.json').read_text())
        measurement = [v for v in raw['records'] if v['phase']=='measurement']
        if len(measurement) != r['summary']['measured_attempts']:
            raise ValueError('measurement count mismatch')
        durations = defaultdict(list)
        for s in spans:
            if s['request_id'].startswith('measurement-'):
                durations[s['stage']].append(s['inclusive_ns']/1e6)
                if s['stage']=='http_route' and s['request_id'].endswith(':admission'):
                    durations['admission_route'].append(s['inclusive_ns']/1e6)
        proxy_values = proxy_intervals(spans)
        for v in proxy_values:
            for metric in ['pre_upstream_ms','upstream_ms','post_upstream_ms']:
                durations[metric].append(v[metric])
        def cgroup_values(key):
            def parse(value):
                return {line.split()[0]: int(line.split()[1]) for line in value.splitlines()}
            before, after = parse(r['cgroup_before'].get(key,'')), parse(r['cgroup_after'].get(key,''))
            return {k: after[k]-before.get(k,0) for k in after}
        cell_rows.append({'index':index, 'kind':r['kind'],'mode':r['mode'], 'concurrency':r['concurrency'],
                          'repeat':r.get('repeat'), 'summary':r['summary'],
                          'cpu_delta':cgroup_values('cpu.stat'), 'memory_delta':cgroup_values('memory.events'),
                          'span_count':len(spans),
                          'business_transform_p50_ms':percentile([v['business_transform_ns']/1e6 for v in measurement if v['success']],.5)})
        stage_rows.append({'index': index, 'kind': r['kind'], 'mode': r['mode'], 'instrument': r['instrument'],
                           'concurrency':r['concurrency'],'repeat':r.get('repeat'),
                           'proxy_intervals': {tool: {metric: distribution([v[metric] for v in proxy_values if v['tool']==tool])
                                                     for metric in ['pre_upstream_ms', 'upstream_ms', 'post_upstream_ms']}
                                               for tool in ['read', 'write']},
                           'stages': {k: {'n': len(v), 'p50_ms': percentile(v,.5), 'p95_ms': percentile(v,.95)} for k,v in durations.items()}})
    result['stage_rows'] = stage_rows
    result['cell_rows'] = cell_rows
    result['groups'] = []
    for kind in ['issue','pr']:
        for concurrency in sorted({r['concurrency'] for r in cell_rows}):
            for mode in ['G0','G2']:
                group = [r for r in cell_rows if (r['kind'],r['concurrency'],r['mode'])==(kind,concurrency,mode)]
                if not group:
                    continue
                stage_group = [r for r in stage_rows if (r['kind'],r['concurrency'],r['mode'])==(kind,concurrency,mode)]
                metrics = {metric:distribution([r['summary']['workflow'][metric] for r in group]) for metric in ['p50_ms','p95_ms']}
                metrics['completion_per_second'] = distribution([r['summary']['completion_rate_inside_window'] for r in group])
                metrics['attempts'] = sum(r['summary']['measured_attempts'] for r in group)
                metrics['errors'] = sum(r['summary']['errors'] for r in group)
                metrics['business_transform_p50_ms'] = distribution([r['business_transform_p50_ms'] for r in group])
                metrics['cpu_throttled_usec'] = sum(r['cpu_delta'].get('throttled_usec',0) for r in group)
                metrics['memory_oom_kill'] = sum(r['memory_delta'].get('oom_kill',0) for r in group)
                stages = sorted({s for r in stage_group for s in r['stages']})
                metrics['stage_p50_ms'] = {s:distribution([r['stages'][s]['p50_ms'] for r in stage_group if s in r['stages']]) for s in stages}
                result['groups'].append({'kind':kind,'concurrency':concurrency,'mode':mode,**metrics})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = analyze(args.folder)
    if args.output:
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k:v for k,v in report.items() if k not in ['stage_rows','cell_rows','pairs']}, ensure_ascii=False, indent=2))
