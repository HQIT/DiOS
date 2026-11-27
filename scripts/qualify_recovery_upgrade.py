"""Measure clean recovery after a failed fixed-image upgrade, using native workloads and mTLS."""
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();os.chdir(ROOT)
    images=json.loads(args.images_file.read_text())
    for key,value in images['images'].items():
        actual=subprocess.check_output(['docker','image','inspect',value,'--format','{{.Id}}'],text=True).strip()
        assert actual==value,(key,value)
        os.environ['DIOS_TEST_RECOVERY_'+key.upper()]=value
    os.environ['DIOS_TEST_DOCKER']='1'
    from test_recovery_upgrade_qualification import OBSERVATIONS
    from test_installation_activation_qualification import OBSERVATIONS as RECOVERY
    suite='test_recovery_upgrade_qualification.RecoveryUpgradeQualificationTests'
    started=time.monotonic()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(suite))
    sources=['backend/tests/test_recovery_upgrade_qualification.py',
        'backend/tests/test_installation_activation_qualification.py','scripts/qualify_recovery_upgrade.py',
        'backend/app/services/installation_activation.py','backend/app/services/installation_readiness.py',
        'backend/app/services/installation_backup.py','backend/app/services/installation_restore.py',
        'backend/app/runtime/restore_remote.py','backend/app/runtime/restore_client.py']
    report={'work_item':'K02-3.03','passed':result.wasSuccessful() and len(OBSERVATIONS)==len(RECOVERY)==1 and not result.skipped,
        'tests':result.testsRun,'errors':len(result.errors),'failures':len(result.failures),'skipped':len(result.skipped),
        'duration_seconds':round(time.monotonic()-started,3),'selected_suite':suite,
        'implementation_base':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'build':images,'observations':OBSERVATIONS,'recovery':RECOVERY,
        'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in sources},
        'failure_cases':[{'test':case.id(),'traceback':trace} for case,trace in result.errors+result.failures],
        'limits':['One Linux/WSL host, SQLite, small synthetic workload and compatible schemas; no enterprise RTO target or schema-changing rollback claim.',
            'Native fixed-image API services, independent Worker/CLI processes, native Docker task and real mTLS; no paid model calls.',
            'Source API and recovery gateway are stopped and original executor is confirmed exited; infrastructure fencing is administrator-managed.',
            'Unknown original result is retained and reconciled before activation; unknown external effects still require independent reconciliation.',
            'Measured RTO includes failure detection, restore, reauthentication/reconciliation, offline activation and governed queued-task completion.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('passed','tests','errors','failures','skipped','duration_seconds')}))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())