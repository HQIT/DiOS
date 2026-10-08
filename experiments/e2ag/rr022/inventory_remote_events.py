"""Send a read-only aggregate query to the two authorized DiOS containers.

No remote file creation, copied database, environment dump, event body export,
task creation, model/tool invocation, deployment or service restart.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from run_paired_tool_pilot import file_hash

REMOTE_QUERY = r'''
import collections,json,sqlite3
c=sqlite3.connect('file:/data/dios.db?mode=ro',uri=True,timeout=5)
c.execute('PRAGMA query_only=ON')
c.execute('PRAGMA busy_timeout=5000')
c.execute('BEGIN')
tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
out={'read_only':True,'database':'/data/dios.db','event_table_present':'event_logs' in tables}
if 'event_logs' in tables:
    cols={r[1] for r in c.execute('PRAGMA table_info(event_logs)')}
    out['event_count']=c.execute('SELECT COUNT(*) FROM event_logs').fetchone()[0]
    if 'created_at' in cols:
        out['event_time_range']=c.execute('SELECT MIN(created_at),MAX(created_at) FROM event_logs').fetchone()
    out['type_counts']=dict(c.execute('SELECT event_type,COUNT(*) FROM event_logs GROUP BY event_type'))
    out['status_counts']=dict(c.execute('SELECT status,COUNT(*) FROM event_logs GROUP BY status'))
    where="source='github/HQIT/qame'"
    out['qame_event_count']=c.execute('SELECT COUNT(*) FROM event_logs WHERE '+where).fetchone()[0]
    # Only scalar metadata is selected from JSON. Never fetch the full payload.
    sql="""SELECT id,created_at,event_type,subject,
      json_extract(cloud_event,'$.id'),json_extract(cloud_event,'$.data.ref'),
      json_extract(cloud_event,'$.data.after'),json_extract(cloud_event,'$.data.delivery_id')
      FROM event_logs WHERE source='github/HQIT/qame' AND json_valid(cloud_event)"""
    records=c.execute(sql).fetchall()
    out['qame_metadata_valid_rows']=len(records)
    out['qame_branch_counts']=dict(collections.Counter((r[5] or r[3] or '(absent)') for r in records))
    out['qame_distinct_cloudevent_ids']=len({r[4] for r in records if r[4]})
    out['qame_rows_with_delivery_id']=sum(bool(r[7]) for r in records)
    out['qame_rows_with_push_sha']=sum(bool(r[6]) for r in records)
    out['qame_distinct_push_ref_sha']=len({(r[5],r[6]) for r in records if r[5] and r[6]})
    experimental=[r for r in records if 'e2ag' in str(r[5] or r[3] or '').lower()]
    candidates=[r for r in records if r not in experimental and r[2]=='git.push' and r[5] and r[6]]
    out['qame_e2ag_named_branch_rows']=len(experimental)
    out['candidate_push_rows_before_provenance_review']=len(candidates)
    out['candidate_distinct_ref_sha_before_provenance_review']=len({(r[5],r[6]) for r in candidates})
    out['candidate_time_range']=([min(r[1] for r in candidates),max(r[1] for r in candidates)] if candidates else [])
    out['qame_event_task_link_count']=0
    if 'a2a_tasks' in tables:
        taskcols={r[1] for r in c.execute('PRAGMA table_info(a2a_tasks)')}
        if 'context_id' in taskcols:
            out['qame_event_task_link_count']=c.execute("SELECT COUNT(DISTINCT e.id) FROM event_logs e JOIN a2a_tasks t ON t.context_id=e.id WHERE e.source='github/HQIT/qame'").fetchone()[0]
    if 'audit_chain' in cols:
        out['qame_nonempty_audit_rows']=c.execute("SELECT COUNT(*) FROM event_logs WHERE source='github/HQIT/qame' AND json_valid(audit_chain) AND json_array_length(audit_chain)>0").fetchone()[0]
out['interpretation']='Candidate metadata only; not verified natural provenance, independent labels or frozen holdout.'
c.close()
print(json.dumps(out,ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("New output required")
    rows = []
    for name in ("dios-e2ag-backend", "dios-backend-1"):
        command = ["wsl.exe", "-e", "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "demogo",
                   f"docker exec -i {name} python -"]
        run = subprocess.run(command, input=REMOTE_QUERY, capture_output=True, text=True, encoding="utf-8", timeout=45)
        try:
            observed = json.loads(run.stdout) if run.returncode == 0 else None
        except ValueError:
            observed = None
        rows.append({"container": name, "exit_code": run.returncode, "observed": observed, "stderr": run.stderr})
        print(json.dumps({"container": name, "exit_code": run.returncode, "observed": observed},ensure_ascii=False), flush=True)
    output = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "authorized DiOS host; aggregate read-only SQL",
              "script_sha256": file_hash(Path(__file__)), "rows": rows,
              "exclusions": "qame e2ag-named branches excluded heuristically; provenance and prior-use review still required",
              "remote_mutations": False, "payload_exported": False}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    if any(r["observed"] is None for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
