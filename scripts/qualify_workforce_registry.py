"""Installed upper-platform package and affected generic contract qualification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--native-image',help='existing Docker image for isolated JSONL/ACP joint acceptance')
    args=parser.parse_args(); output=Path(args.output).resolve(); output.mkdir(parents=True,exist_ok=True)
    os.environ['PYTHONPATH']=str(ROOT/'backend')+os.pathsep+os.environ.get('PYTHONPATH','')
    import dios_workforce.registry as installed
    assert not Path(installed.__file__).resolve().is_relative_to(ROOT/'applications'), 'Install the wheel first'
    sys.path[:0]=[str(ROOT/'applications/workforce-registry/tests'),str(ROOT/'backend'),
                  str(ROOT/'backend/tests'),str(ROOT/'sdk/python/tests')]
    names=['test_registry','test_runtime_integration','test_workitems','test_rework','test_groups','test_interventions','test_compensations','test_takeover_joint']
    if args.native_image:
        os.environ['DIOS_WORKITEM_TEST_IMAGE']=args.native_image
        names.append('test_workitem_joint_native')
    suite=unittest.TestLoader().loadTestsFromNames(names)
    cases=[]
    class Result(unittest.TextTestResult):
        def addSuccess(self,test):
            super().addSuccess(test);cases.append({'test':test.id(),'state':'passed'})
        def addFailure(self,test,err):
            super().addFailure(test,err);cases.append({'test':test.id(),'state':'failed'})
        def addError(self,test,err):
            super().addError(test,err);cases.append({'test':test.id(),'state':'error'})
    start=time.time()
    with (output/'tests.log').open('w') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2,resultclass=Result).run(suite)
    sources=['applications/workforce-registry/src/dios_workforce/registry.py',
             'applications/workforce-registry/src/dios_workforce/cli.py',
             'applications/workforce-registry/src/dios_workforce/workitems.py',
             'applications/workforce-registry/src/dios_workforce/groups.py',
             'applications/workforce-registry/src/dios_workforce/interventions.py',
             'applications/workforce-registry/src/dios_workforce/compensations.py',
             'applications/workforce-registry/src/dios_workforce/work_cli.py',
             'applications/workforce-registry/tests/test_registry.py',
             'applications/workforce-registry/tests/test_runtime_integration.py',
             'applications/workforce-registry/tests/test_workitems.py',
             'applications/workforce-registry/tests/test_rework.py',
             'applications/workforce-registry/tests/test_groups.py',
             'applications/workforce-registry/tests/test_interventions.py',
             'applications/workforce-registry/tests/test_compensations.py',
             'applications/workforce-registry/tests/test_takeover_joint.py',
             'applications/workforce-registry/tests/test_workitem_joint_native.py',
             'applications/workforce-registry/tests/workitem_native_fixture.py',
             'backend/app/api/os/runtime_tasks.py','backend/app/services/task_console.py',
             'backend/app/runtime/application_contracts.py','sdk/python/src/dios_sdk/tasks.py','cli/dios',
             'scripts/qualify_workforce_registry.py']
    report={'result':'PASS' if result.wasSuccessful() and not result.skipped else 'FAIL',
        'tests_run':result.testsRun,'skipped':len(result.skipped),'duration_seconds':round(time.time()-start,3),
        'application':'dios-workforce-registry==0.6.0','sdk':'dios-sdk==0.1.22','cases':cases,
        'installed_module_sha256':hashlib.sha256(Path(installed.__file__).read_bytes()).hexdigest(),
        'source_hashes':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in sources},
        'conditions':{'identity':'synthetic dedicated principals; independent application SQLite',
            'worker':'real Worker; deterministic adapter plus isolated Docker JSONL/ACP processes when native-image is set; no model call',
            'compensation':'explicit independent work, live source settlement, E2AG dispatch and approval, fresh artifact access and separate human decisions',
            'authorization':'actual HTTP middleware, E2AG admission/execution/result verifier, admin CLI subprocess',
            'compatibility':'immutable template and revision; generic profile precondition at admission',
            'workitem':'durable business identity, pinned employee, explicit reviewer decision, live object authorization and integrity checks',
            'rework':'immutable prior-round snapshots; distinct Run and Work authorization per round; legacy pending-key upgrade and durable rework receipt',
            'interventions':'durable takeover intent, confirmed cancellation before human ownership, current-authority handback and versioned runtime approval projection',
            'groups':'fixed one-level child set, distinct employee identities, separate parent reviewer, live per-Run artifact checks and explicit aggregate decision'},
        'not_qualified':['enterprise directory/SSO','physical Runtime replacement','cross-Harness live switch',
                         'multiuser application web UI and operator role model',
                         'recursive decomposition and parent replanning']}
    if args.native_image:
        from test_workitem_joint_native import OBSERVATIONS
        report['native_joint']={'image':args.native_image,'observations':OBSERVATIONS}
    else:
        report['not_qualified'].append('native Docker joint acceptance; run with --native-image')
    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('result','tests_run','skipped','duration_seconds')}))
    return 0 if report['result']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
