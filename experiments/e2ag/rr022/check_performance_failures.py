"""Audit failed pretrial requests from saved ledgers/DB; never re-execute."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    source = json.loads(args.input.read_text(encoding='utf-8'))
    assert source['status'] != 'running'
    out = dict(status='read_only_failure_audit', input_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest(), cells=[], failures=[])
    for cell in source['rows']:
        directory = Path(cell['directory'])
        raw = json.loads((directory/'client-raw.json').read_text(encoding='utf-8'))
        spans = json.loads((directory/'server-spans.json').read_text(encoding='utf-8'))
        operations = [json.loads(line) for line in (directory/'operations.jsonl').read_text(encoding='utf-8').splitlines()]
        reports = [json.loads(line) for line in (directory/'reports.jsonl').read_text(encoding='utf-8').splitlines()]
        log = (directory/'app.log').read_text(encoding='utf-8')
        counts = Counter((r['phase'],r['success']) for r in raw['records'])
        out['cells'].append(dict(kind=cell['kind'],mode=cell['mode'],concurrency=cell['concurrency'],
            proxy_transport=cell.get('proxy_transport','per-call'),
            warmup_attempts=sum(n for (phase,_),n in counts.items() if phase=='warmup'),warmup_failures=counts['warmup',False],
            measured_attempts=sum(n for (phase,_),n in counts.items() if phase=='measurement'),measured_failures=counts['measurement',False],
            sqlite_locked_messages=log.count('sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked'),
            app_log_sha256=hashlib.sha256(log.encode()).hexdigest()))
        with sqlite3.connect(directory/'state.db') as db:
            has_intents = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='e2ag_tool_invocations'").fetchone())
            for row in raw['records']:
                if row['success']: continue
                rid=row['request_id']
                matching_ops=[op for op in operations if op['rpc_id'].rsplit(':',1)[0]==rid]
                matching_reports=[report for report in reports if report['rpc_id'].rsplit(':',1)[0]==rid]
                event_id=row.get('event_id')
                if not event_id:
                    candidates={s['event_id'] for s in spans if s['request_id']==rid+':admission' and 'event_id' in s}
                    assert len(candidates)<=1
                    event_id=next(iter(candidates),None)
                events=list(db.execute('SELECT id,audit_chain FROM event_logs WHERE id=?',(event_id,))) if event_id else []
                chain=json.loads(events[0][1]) if events else []
                tool_entries=[e for e in chain if e['stage']=='tool_call']
                upstream=Counter(op['tool'] for op in matching_ops)
                audit=Counter(e['evidence'].get('tool') for e in tool_entries)
                missing=dict(upstream-audit) if cell['mode']=='G2' else None
                invocation_rows = list(db.execute('SELECT id,tool_name,state FROM e2ag_tool_invocations WHERE event_log_id=?', (event_id,))) if has_intents and event_id else []
                intents = Counter(i[1] for i in invocation_rows)
                missing_intent = dict(upstream-intents) if has_intents and cell['mode']=='G2' else None
                out['failures'].append(dict(kind=cell['kind'],mode=cell['mode'],concurrency=cell['concurrency'],
                    proxy_transport=cell.get('proxy_transport','per-call'),
                    request_id=rid,phase=row['phase'],admission_status=row.get('admission_status'),
                    call_statuses=[dict(tool=c['tool'],status=c['status'], error_data=c.get('error_data')) for c in row['calls']],
                    error=row.get('error'),event_id=event_id,event_attribution_known=bool(event_id),
                    upstream_operations=dict(upstream),persisted_reports=len(matching_reports),
                    persisted_tool_audit=dict(audit),upstream_operations_without_tool_audit=missing,
                    invocation_schema_present=has_intents, invocation_states=dict(Counter(i[2] for i in invocation_rows)),
                    upstream_operations_without_durable_intent=missing_intent))
    out['summary']=dict(failed_requests=len(out['failures']),
        measured_failed_requests=sum(r['phase']=='measurement' for r in out['failures']),
        warmup_failed_requests=sum(r['phase']=='warmup' for r in out['failures']),
        failed_requests_with_upstream_activity=sum(bool(r['upstream_operations']) for r in out['failures']),
        failed_requests_with_persisted_reports=sum(r['persisted_reports']>0 for r in out['failures']),
        failed_g2_requests_with_audit_gap=sum(bool(r['upstream_operations_without_tool_audit']) for r in out['failures']),
        failed_g2_requests_with_missing_intent=sum(bool(r['upstream_operations_without_durable_intent']) for r in out['failures']),
        failed_requests_with_durable_unknown=sum(r['invocation_states'].get('outcome_unknown', 0)>0 for r in out['failures']))
    args.output.write_text(json.dumps(out,ensure_ascii=True,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(out['summary']))


if __name__=='__main__': main()
