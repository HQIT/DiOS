"""Isolated Linux qualification of clean installation recovery and guarded activation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'backend/tests')]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--native-only',action='store_true')
    args=parser.parse_args()
    os.chdir(ROOT)
    os.environ['DIOS_TEST_DOCKER']='1'
    from test_installation_activation_qualification import OBSERVATIONS
    names=['test_installation_activation_qualification.InstallationActivationQualificationTests']
    if not args.native_only:names[:0]=['test_installation_activation.ActivationTests','test_installation_readiness.ReadinessTests']
    started=time.monotonic()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
    sources=['backend/app/services/installation_activation.py','backend/app/runtime/cli.py',
        'backend/app/services/installation_readiness.py','backend/app/services/installation_restore.py',
        'backend/app/runtime/restore_remote.py','backend/app/runtime/restore_client.py',
        'backend/tests/test_installation_activation.py','backend/tests/test_installation_activation_qualification.py',
        'scripts/qualify_installation_activation.py']
    value={'work_item':'K02-3.02','passed':result.wasSuccessful() and len(OBSERVATIONS)==1 and not result.skipped,
        'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'duration_seconds':round(time.monotonic()-started,3),'selected_suites':names,
        'implementation_base':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources},
        'native':OBSERVATIONS,'failure_cases':[{'test':case.id(),'traceback':trace} for case,trace in result.errors+result.failures],
        'limits':['One Linux/WSL host with independent CLI Worker processes, native Docker workloads and loopback mTLS.',
            'Source isolation uses explicit shutdown of the fixture listener/executor and no restart supervisor; infrastructure must fence production sources.',
            'The original read-only workload finishes after the checkpoint; its retained exit receipt and immutable output are reconciled without relaunch.',
            'External side effects with unknown outcome remain held for administrator reconciliation; exactly-once external effects are not asserted.',
            'SQLite installation qualification; PostgreSQL/shared-backend recovery remains D01-2.03.',
            'Manual administrative evidence is attested, not automatically verified infrastructure fencing.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({key:value[key] for key in ('passed','tests','failures','errors','skipped','duration_seconds')}))
    return 0 if value['passed'] else 1


if __name__=='__main__':raise SystemExit(main())