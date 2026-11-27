"""Fixed-image clean installation and schema-changing cutover qualification."""
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
    build=json.loads(args.images_file.read_text());images=build['images']
    for role,variable in [('old','DIOS_TEST_UPGRADE_OLD'),('new','DIOS_TEST_UPGRADE_NEW'),('bad','DIOS_TEST_UPGRADE_BAD'),('frontend','DIOS_TEST_FRONTEND_IMAGE'),('new','DIOS_TEST_ACP_IMAGE')]:
        value=images[role]
        assert subprocess.check_output(['docker','image','inspect',value,'--format','{{.Id}}'],text=True).strip()==value
        os.environ[variable]=value
    os.environ['DIOS_TEST_DOCKER']='1'
    from test_installation_schema_upgrade import OBSERVATIONS
    from test_reference_upgrade import OBSERVATIONS as CUTOVER
    suite='test_installation_schema_upgrade.SchemaInstallationUpgradeTests'
    start=time.monotonic()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(suite))
    source_files=['backend/tests/test_installation_schema_upgrade.py','backend/tests/test_reference_upgrade.py',
        'scripts/qualify_installation_schema.py','backend/app/runtime/database.py','backend/app/runtime/technical_release_schema.py',
        'deploy/linux/install.py','deploy/linux/compose.yaml']
    report={'work_item':'D01-1.02','passed':result.wasSuccessful() and len(OBSERVATIONS)==len(CUTOVER)==1 and not result.skipped,
        'tests':result.testsRun,'errors':len(result.errors),'failures':len(result.failures),'skipped':len(result.skipped),
        'duration_seconds':round(time.monotonic()-start,3),'selected_suite':suite,'build':build,
        'implementation_base':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'schema_changes':OBSERVATIONS,'cutover':CUTOVER,
        'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in source_files},
        'failure_cases':[{'test':case.id(),'traceback':trace} for case,trace in result.errors+result.failures],
        'limits':['Linux/WSL local Docker with SQLite and immutable images; three native ACP processes use a synthetic model peer.',
            'The real historical upgrade adds the technical-release ledger and guards; arbitrary destructive or incompatible schema rollback is not asserted.',
            'Real Cursor/DSH installation contracts and Linux unattended environment evidence are referenced separately after hash verification.',
            'Shared backend and physical multi-node qualification remain their existing WBS items.']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('passed','tests','errors','failures','skipped','duration_seconds')}))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())