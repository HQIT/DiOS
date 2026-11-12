"""Run backend regression suite and emit bounded, credential-free acceptance evidence."""
import argparse,json,os,platform,sys,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path);args=ap.parse_args()
    os.chdir(ROOT/'backend')
    suite=unittest.defaultTestLoader.discover('tests')
    started=time.monotonic()
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    import sqlalchemy
    worker_module=sys.modules.get('test_runtime_worker') or sys.modules.get('tests.test_runtime_worker')
    report={'scope':'runtime foundation and existing backend regression; isolated execution fixtures; no production migration or Agent certification',
        'python':platform.python_version(),'sqlalchemy':sqlalchemy.__version__,'platform':platform.system(),
        'postgresql_enabled':bool(os.environ.get('DIOS_TEST_POSTGRES_URL')),
        'docker_worker_enabled':os.environ.get('DIOS_TEST_DOCKER')=='1',
        'worker_concurrency_evidence':getattr(worker_module,'OBSERVED_CONCURRENCY',[]),
        'tests_run':result.testsRun,'skipped':len(result.skipped),'failures':len(result.failures),'errors':len(result.errors),
        'duration_seconds':round(time.monotonic()-started,3),'successful':result.wasSuccessful(),
        'cases':[name for name,_ in result.skipped],
        'limits':['PostgreSQL single isolated node; no HA/capacity claim','Windows process-tree and actual Agent tests pending','Legacy scheduler takeover, automatic circuit recovery, API authorization and UI pending']}
    if args.output:
        output=args.output if args.output.is_absolute() else ROOT/args.output
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1
if __name__=='__main__':raise SystemExit(main())
