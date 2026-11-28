"""Business completion belongs to the upper platform, independent of Run status."""
import hashlib
import json
import time

from .registry import check, digest, encoded, identifier, principal


class WorkItems:
    """One trusted CLI operation at a time under the Registry process lock."""
    def __init__(self, registry):
        self.registry, self.db = registry, registry.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY, specification TEXT NOT NULL, reviewer TEXT NOT NULL,
                state TEXT NOT NULL, version INTEGER NOT NULL, employee TEXT, employee_revision INTEGER,
                run_id TEXT, evidence TEXT, decision TEXT);
            CREATE TABLE IF NOT EXISTS work_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, work_id TEXT NOT NULL,
                kind TEXT NOT NULL, details TEXT NOT NULL, created REAL NOT NULL);
        ''')

    def event(self, work_id, kind, details):
        self.db.execute('INSERT INTO work_events(work_id,kind,details,created) VALUES (?,?,?,?)',
                        (work_id, kind, encoded(details), time.time()))

    def get(self, work_id):
        row=self.db.execute('SELECT * FROM work_items WHERE id=?',(work_id,)).fetchone()
        check(row is not None,'WORK_ITEM_NOT_FOUND')
        value=dict(row)
        for field in ('specification','reviewer','evidence','decision'):
            if value[field] is not None: value[field]=json.loads(value[field])
        return value

    def history(self, work_id):
        return {'work_item':self.get(work_id),'events':[dict(r) for r in self.db.execute(
            'SELECT * FROM work_events WHERE work_id=? ORDER BY sequence',(work_id,))]}

    async def create(self, work_id, title, kind, sample, goal, reviewer):
        identifier(work_id);identifier(kind);identifier(sample)
        check(isinstance(title,str) and 0<len(title)<=500,'INVALID_TITLE')
        check(isinstance(goal,str) and 0<len(goal)<=8000,'INVALID_GOAL')
        identity=principal(await reviewer.identity())
        spec={'title':title,'kind':kind,'sample':sample,'goal':goal}
        if self.db.execute('SELECT id FROM work_items WHERE id=?',(work_id,)).fetchone():
            old=self.get(work_id)
            check(old['specification']==spec and old['reviewer']==identity,'WORK_ITEM_IDEMPOTENCY_CONFLICT')
            return old
        with self.db:
            self.db.execute("INSERT INTO work_items VALUES (?,?,?,'created',1,NULL,NULL,NULL,NULL,NULL)",
                            (work_id,encoded(spec),encoded(identity)))
            self.event(work_id,'created',{'reviewer':identity})
        return self.get(work_id)

    def bind(self, work_id, employee, *, expected_version):
        row=self.get(work_id)
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state'] in ('created','bound'),'WORK_ITEM_ALREADY_DISPATCHED')
        actor=self.registry.get(employee)
        check(actor['state']=='active','EMPLOYEE_NOT_ACTIVE')
        identity=json.loads(actor['identity'])
        check(identity['subject']!=row['reviewer']['subject'] or identity['issuer']!=row['reviewer']['issuer'],
              'SELF_REVIEW_DENIED')
        check(row['specification']['kind'] in self.registry.revision(employee,actor['active'])['bindings'],
              'WORK_TYPE_UNAVAILABLE')
        with self.db:
            self.db.execute("UPDATE work_items SET state='bound',employee=?,employee_revision=?,version=version+1 WHERE id=?",
                            (employee,actor['active'],work_id))
            self.event(work_id,'bound',{'employee':employee,'revision':actor['active']})
        return self.get(work_id)

    async def dispatch(self, work_id, client, *, expected_version):
        row=self.get(work_id)
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state'] in ('bound','dispatching','running'),'WORK_ITEM_NOT_DISPATCHABLE')
        actor=self.registry.get(row['employee'])
        check(principal(await client.identity())==json.loads(actor['identity']),'EMPLOYEE_IDENTITY_MISMATCH')
        if row['run_id'] is not None: return row
        key='workitem-'+digest(work_id)
        receipt=self.db.execute('SELECT revision FROM submissions WHERE employee=? AND key=?',(row['employee'],key)).fetchone()
        # A durable employee receipt pins the original request after an uncertain response.
        check((receipt['revision'] if receipt else actor['active'])==row['employee_revision'],
              'EMPLOYEE_PUBLICATION_CHANGED')
        if row['state']=='bound':
            with self.db:
                self.db.execute("UPDATE work_items SET state='dispatching' WHERE id=?",(work_id,))
                self.event(work_id,'dispatch_requested',{'key':key})
        spec=row['specification']
        result=await self.registry.submit(row['employee'],spec['kind'],spec['sample'],spec['goal'],key,client)
        with self.db:
            self.db.execute("UPDATE work_items SET state='running',run_id=?,version=version+1 WHERE id=?",(result['run_id'],work_id))
            self.event(work_id,'dispatched',{'run_id':result['run_id']})
        return self.get(work_id)

    async def reviewer(self, row, client):
        check(principal(await client.identity())==row['reviewer'],'REVIEWER_IDENTITY_MISMATCH')

    async def evidence(self, row, client):
        run=await client.get(row['run_id'])
        check(run['id']==row['run_id'],'RUN_IDENTITY_MISMATCH')
        if run['state']!='succeeded': return run,None
        check(bool(run.get('artifacts')),'RESULT_EVIDENCE_REQUIRED')
        refs=[]
        for artifact in run['artifacts']:
            blob=await client.artifact(row['run_id'],artifact['id'])
            check(len(blob)==artifact['size'] and hashlib.sha256(blob).hexdigest()==artifact['digest'],
                  'ARTIFACT_INTEGRITY_FAILED')
            refs.append({k:artifact[k] for k in ('id','digest','size')})
        return run,{'run_id':run['id'],'artifacts':sorted(refs,key=lambda v:v['id'])}

    async def observe(self, work_id, client, *, expected_version):
        row=self.get(work_id)
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state'] in ('running','awaiting_confirmation','execution_failed'),'WORK_ITEM_NOT_OBSERVABLE')
        await self.reviewer(row,client)
        # Timeout or denied access leaves the business state unchanged.
        run,evidence=await self.evidence(row,client)
        state='awaiting_confirmation' if evidence else ('execution_failed' if run['state'] in ('failed','cancelled','canceled') else 'running')
        if state!=row['state'] or evidence!=row['evidence']:
            with self.db:
                self.db.execute('UPDATE work_items SET state=?,evidence=?,version=version+1 WHERE id=?',
                    (state,encoded(evidence) if evidence else None,work_id))
                self.event(work_id,'observed',{'run_id':run['id'],'run_state':run['state'],'evidence':evidence})
        return self.get(work_id)

    async def confirm(self, work_id, client, decision, note, *, expected_version):
        check(decision in ('accept','reject'),'INVALID_DECISION')
        check(isinstance(note,str) and 0<len(note.strip())<=4000,'REVIEW_NOTE_REQUIRED')
        row=self.get(work_id)
        await self.reviewer(row,client)
        value={'decision':decision,'note':note,'reviewer':row['reviewer']}
        if row['decision'] is not None:
            check(row['decision']==value and row['version'] in (expected_version,expected_version+1),'DECISION_CONFLICT')
            return row
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state']=='awaiting_confirmation','WORK_ITEM_NOT_READY_FOR_CONFIRMATION')
        run,evidence=await self.evidence(row,client)
        check(evidence is not None and evidence==row['evidence'],'REVIEW_EVIDENCE_CHANGED')
        with self.db:
            self.db.execute('UPDATE work_items SET state=?,decision=?,version=version+1 WHERE id=?',
                ('confirmed' if decision=='accept' else 'needs_revision',encoded(value),work_id))
            self.event(work_id,'human_decision',dict(value,evidence_digest=digest(evidence)))
        return self.get(work_id)
