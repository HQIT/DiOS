"""D01-1.02: real historical schema upgrade with natural drain and rollback."""
import os
import sqlite3
import unittest
from pathlib import Path

import test_reference_upgrade as reference

OBSERVATIONS=[]


class SchemaInstallationUpgradeTests(reference.NativeInstallationUpgradeTests):
    async def command(self, *args, check=True, timeout=45):
        code,out,err=await super().command(*args,check=False,timeout=timeout)
        if check:self.assertEqual(code,0,(out+err)[-3000:])
        return code,out,err

    async def compose(self, *args, **kwargs):
        if args[:6]==('exec','-T','backend','python','-m','app.runtime.cli') and args[6] in {'installation-backup','installation-verify'}:
            before=self.schema_inventory()
            result=await self.command('env','DIOS_BACKEND_IMAGE='+self.new_image,'docker','compose',
                '--project-directory',str(self.root),'run','--rm','--no-deps','-T',
                '--entrypoint','python','backend','-m','app.runtime.cli',*args[6:],**kwargs)
            self.assertEqual(self.schema_inventory(),before)
            self.target_reader_backup_verified=True
            return result
        return await super().compose(*args,**kwargs)

    def schema_inventory(self):
        path=self.settings['DIOS_RUNTIME_DATABASE_URL'].removeprefix('sqlite+aiosqlite:///')
        with sqlite3.connect(path) as db:
            return {'tables':sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")),
                'triggers':sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")),
                'integrity':db.execute('PRAGMA integrity_check').fetchall(),
                'run_count':db.execute('SELECT count(*) FROM runtime_runs').fetchone()[0],
                'attempt_count':db.execute('SELECT count(*) FROM runtime_attempts').fetchone()[0]}

    async def switch(self,image,success=True):
        if image!=self.new_image or getattr(self,'schema_upgraded',False):
            return await super().switch(image,success)
        from app.runtime.technical_release_schema import metadata, GUARDS
        before=self.schema_inventory()
        self.assertFalse(set(metadata.tables)&set(before['tables']))
        self.assertEqual(before['run_count'],4)
        self.assertEqual(before['attempt_count'],3)
        await self.compose('stop','backend')
        self.env['DIOS_BACKEND_IMAGE']=image
        (self.root/'.env').write_text(''.join(f'{k}={v}\n' for k,v in self.env.items()))
        await self.compose('run','--rm','-T','migrate')
        after=self.schema_inventory()
        self.assertEqual(set(after['tables'])-set(before['tables']),set(metadata.tables))
        self.assertTrue(set(GUARDS)<=set(after['triggers']))
        self.assertEqual(after['integrity'],[('ok',)])
        self.assertEqual(after['run_count'],before['run_count'])
        self.assertEqual(after['attempt_count'],before['attempt_count'])
        self.schema_upgraded=True
        await super().switch(image)
        # An actual candidate startup failure after migration must still permit rollback.
        await super().switch(os.environ['DIOS_TEST_UPGRADE_BAD'],success=False)
        await super().switch(self.old_image)
        self.assertEqual((await self.api('upgrade/status'))['mode'],'draining')
        self.assertEqual(self.schema_inventory(),after)
        await super().switch(image)
        OBSERVATIONS.append({'old_image':self.old_image,'new_image':image,
            'before':before,'after':after,'added_tables':sorted(set(after['tables'])-set(before['tables'])),
            'new_immutable_guards':sorted(GUARDS),'failed_candidate_after_migration_rolled_back':True,
            'old_version_retained_schema_and_data':True,'rollback_kept_admission_drained':True,'target_reader_backup_without_source_migration':self.target_reader_backup_verified})