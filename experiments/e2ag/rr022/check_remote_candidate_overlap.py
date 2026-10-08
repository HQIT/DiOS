"""Read-only comparison of hashed Qame push metadata; no event bodies."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from run_paired_tool_pilot import file_hash

QUERY = r'''
import hashlib,json,sqlite3
c=sqlite3.connect('file:/data/dios.db?mode=ro',uri=True,timeout=5)
c.execute('PRAGMA query_only=ON')
c.execute('BEGIN')
rows=c.execute("SELECT json_extract(cloud_event,'$.data.ref'),json_extract(cloud_event,'$.data.after') FROM event_logs WHERE source='github/HQIT/qame' AND event_type='git.push' AND json_valid(cloud_event)").fetchall()
candidate={(ref,sha) for ref,sha in rows if ref and sha and 'e2ag' not in ref.lower()}
print(json.dumps({'candidate_ref_sha_hashes':sorted(hashlib.sha256(json.dumps(r).encode()).hexdigest() for r in candidate)}))
c.close()
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    assert not a.output.exists()
    rows=[]
    for name in ('dios-e2ag-backend','dios-backend-1'):
        run=subprocess.run(['wsl.exe','-e','ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','demogo',f'docker exec -i {name} python -'],
                           input=QUERY,capture_output=True,text=True,encoding='utf-8',timeout=30,check=True)
        rows.append({'container':name,**json.loads(run.stdout)})
    x,y=[set(r['candidate_ref_sha_hashes']) for r in rows]
    result={'observed_at_utc':datetime.now(timezone.utc).isoformat(),'rows':rows,'unique_candidates_across_containers':len(x|y),
            'overlap':len(x&y),'identity_rule':'Qame push ref + after SHA, not proof of event provenance or independent delivery',
            'script_sha256':file_hash(Path(__file__)),'remote_mutations':False,'payload_exported':False}
    with a.output.open('x',encoding='utf-8') as f:
        json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'unique_candidates_across_containers':len(x|y),'overlap':len(x&y)}))


if __name__=='__main__':
    main()
