"""Verify current-scope closure using retained evidence; no model or fault execution."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
from verify_reference_harness import verify

ROOT=Path(__file__).resolve().parents[1]


def main():
    report=json.loads((ROOT/'docs/design/evidence/reference-closure-20261128.json').read_text())
    expected={f'R{i:02}' for i in range(1,19)}-{'R01','R08','R16','R17','R18'}
    covered=set()
    for ref in report['reused_and_new_evidence']:
        path=ROOT/ref['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==ref['sha256'],ref['path']
        covered.update(ref['covers'])
    assert covered==expected|{f'G{i:02}' for i in range(1,6)}
    assert set(report['qualified_core_cases'])==expected
    assert set(report['application_acceptance'])=={'R01','R08','R16','R17','R18'}
    assert set(report['future_candidates'])=={'G06'}
    assert report['full_original_suite_passed'] is False
    proof=report['reference_paths'];path=ROOT/proof['report']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==proof['sha256']
    with contextlib.redirect_stdout(io.StringIO()):
        verify(path)
    g05=json.loads((ROOT/'docs/design/evidence/human-takeover-joint-20261128.json').read_text())
    assert g05['result']=='PASS' and len(g05['observations'])==1
    for path,sha in g05['source_hashes'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==sha,path
    print(json.dumps({'current_core_scope':'PASS','core_faults':13,'governance':5,
        'reference_paths':6,'application_cases_pending':5,'future_candidates':1,'new_model_calls':0}))


if __name__=='__main__':main()
