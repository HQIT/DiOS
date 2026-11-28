"""Upper-platform ownership and runtime approval references; no authority issuance."""
import json
import time

from .registry import check, encoded, identifier, principal, validate


class Interventions:
    def __init__(self, work):
        self.work, self.db, self.registry = work, work.db, work.registry

    def history(self, work_id):
        takeovers=[]
        for row in self.db.execute('SELECT * FROM work_takeovers WHERE work_id=? ORDER BY round_number',(work_id,)):
            value=dict(row);value['request']=json.loads(value['request']);takeovers.append(value)
        approvals=[dict(round_number=r['round_number'], observed_at=r['observed'], **json.loads(r['snapshot']))
            for r in self.db.execute('SELECT * FROM work_approvals WHERE work_id=? ORDER BY round_number,invocation_id',(work_id,))]
        sync=[dict(round_number=r['round_number'], **json.loads(r['observation'])) for r in self.db.execute(
            'SELECT * FROM work_approval_sync WHERE work_id=? ORDER BY round_number',(work_id,))]
        return dict(takeovers=takeovers,approvals=approvals,approval_observations=sync)

    async def takeover(self, work_id, client, employee_client, reason, key, *, expected_version):
        identifier(key)
        check(isinstance(reason,str) and 0<len(reason.strip())<=4000,'TAKEOVER_REASON_REQUIRED')
        row=self.work.get(work_id);await self.work.reviewer(row,client)
        request=encoded(dict(key=key,reason=reason,expected_version=expected_version,operator=row['reviewer']))
        old=self.db.execute('SELECT * FROM work_takeovers WHERE work_id=? AND round_number=?',
                            (work_id,row['round_number'])).fetchone()
        if old:
            check(old['request']==request,'TAKEOVER_CONFLICT')
            return await self.reconcile(work_id,client,employee_client,expected_version=row['version'])
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state'] in ('bound','running','execution_failed','awaiting_confirmation','needs_revision'),
              'WORK_ITEM_NOT_TAKEOVER_READY')
        if row['run_id']:
            run=await client.get(row['run_id']);check(run['id']==row['run_id'],'RUN_IDENTITY_MISMATCH')
        with self.db:
            self.db.execute('INSERT INTO work_takeovers VALUES (?,?,?,\'pending\',NULL)',
                            (work_id,row['round_number'],request))
            self.db.execute("UPDATE work_items SET state='takeover_pending',version=version+1 WHERE id=?",(work_id,))
            self.work.event(work_id,'takeover_requested',dict(json.loads(request),run_id=row['run_id']))
        return await self.reconcile(work_id,client,employee_client,expected_version=expected_version+1)

    async def reconcile(self, work_id, client, employee_client, *, expected_version):
        row=self.work.get(work_id);await self.work.reviewer(row,client)
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state'] in ('takeover_pending','human_owned'),'TAKEOVER_NOT_PENDING')
        if row['state']=='human_owned':return row
        run_state='not_dispatched'
        if row['run_id']:
            run=await client.get(row['run_id']);check(run['id']==row['run_id'],'RUN_IDENTITY_MISMATCH')
            if run['state'] not in ('succeeded','failed','cancelled') and not run.get('cancel_requested'):
                # The application controller uses the same scoped employee as dispatch.
                actor=self.registry.get(row['employee'])
                check(principal(await employee_client.identity())==json.loads(actor['identity']),'EMPLOYEE_IDENTITY_MISMATCH')
                run=await employee_client.cancel(row['run_id'],expected_version=run['version'])
                check(run['id']==row['run_id'],'RUN_IDENTITY_MISMATCH')
            run_state=run['state']
        terminal=run_state in ('not_dispatched','succeeded','failed','cancelled')
        old=self.db.execute('SELECT run_state FROM work_takeovers WHERE work_id=? AND round_number=?',
                            (work_id,row['round_number'])).fetchone()
        with self.db:
            self.db.execute('UPDATE work_takeovers SET status=?,run_state=? WHERE work_id=? AND round_number=?',
                            ('owned' if terminal else 'pending',run_state,work_id,row['round_number']))
            if terminal:
                self.db.execute("UPDATE work_items SET state='human_owned',version=version+1 WHERE id=?",(work_id,))
            if terminal or old['run_state']!=run_state:
                self.work.event(work_id,'takeover_confirmed' if terminal else 'takeover_waiting',
                                dict(run_id=row['run_id'],run_state=run_state,operator=row['reviewer']))
        return self.work.get(work_id)

    async def handback(self, work_id, reviewer, employee_client, goal, note, key, *, expected_version):
        identifier(key)
        check(isinstance(goal,str) and 0<len(goal.strip())<=8000,'INVALID_GOAL')
        check(isinstance(note,str) and 0<len(note.strip())<=4000,'HAND_BACK_NOTE_REQUIRED')
        row=self.work.get(work_id);await self.work.reviewer(row,reviewer)
        request=encoded(dict(goal=goal,note=note,expected_version=expected_version))
        old=self.db.execute('SELECT * FROM work_handbacks WHERE work_id=? AND request_key=?',(work_id,key)).fetchone()
        if old:
            check(old['request']==request,'HAND_BACK_CONFLICT');return json.loads(old['receipt'])
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state']=='human_owned','TAKEOVER_NOT_SETTLED')
        if row['run_id']:
            run=await reviewer.get(row['run_id'])
            check(run['id']==row['run_id'] and run['state'] in ('succeeded','failed','cancelled'),'TAKEOVER_NOT_SETTLED')
        actor=self.registry.get(row['employee']);check(actor['state']=='active','EMPLOYEE_NOT_ACTIVE')
        definition=self.registry.revision(row['employee'],actor['active'])
        check(row['specification']['kind'] in definition['bindings'],'WORK_TYPE_UNAVAILABLE')
        await validate(definition,employee_client,json.loads(actor['identity']))
        with self.db:
            self.db.execute('INSERT INTO work_rounds VALUES (?,?,?)',(work_id,row['round_number'],encoded(row)))
            self.db.execute("""UPDATE work_items SET state='bound',version=version+1,round_number=round_number+1,
                round_goal=?,employee_revision=?,run_id=NULL,evidence=NULL,decision=NULL WHERE id=?""",
                (goal,actor['active'],work_id))
            self.db.execute("UPDATE work_takeovers SET status='handed_back' WHERE work_id=? AND round_number=?",
                            (work_id,row['round_number']))
            self.work.event(work_id,'handed_back',dict(key=key,goal=goal,note=note,operator=row['reviewer'],
                previous_round=row['round_number'],previous_run_id=row['run_id']))
            receipt=self.work.get(work_id)
            self.db.execute('INSERT INTO work_handbacks VALUES (?,?,?,?)',(work_id,key,request,encoded(receipt)))
        return receipt

    async def sync_approvals(self, work_id, client, *, expected_version, round_number=None, invocation_id=None):
        row=self.work.get(work_id);await self.work.reviewer(row,client)
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        number=row['round_number'] if round_number is None else round_number
        source=next((r for r in self.work.history(work_id)['rounds'] if r['round_number']==number),None)
        check(source is not None and source['run_id'],'ROUND_RUN_REQUIRED')
        run_id=source['run_id'];observed=time.time()
        try:
            page=({'items':[await client.tool(run_id,invocation_id)],'at_limit':True}
                  if invocation_id else await client.tools(run_id))
            check(not page.get('migration_required'),'TOOL_MIGRATION_REQUIRED')
            check(isinstance(page.get('items'),list),'INVALID_APPROVAL_RESPONSE')
            values=[]
            fields=('id','run_id','attempt_id','session_id','tool_id','arguments_digest','state','version',
                    'approval_state','approver','approval_reason','expires_at','expired','reason')
            for item in page['items']:
                check(item['run_id']==run_id and (not invocation_id or item['id']==invocation_id),'APPROVAL_RUN_MISMATCH')
                value={k:item[k] for k in fields};identifier(value['id'])
                check(type(value['version']) is int,'INVALID_APPROVAL_VERSION');values.append(value)
            with self.db:
                for value in values:
                    old=self.db.execute('SELECT snapshot FROM work_approvals WHERE work_id=? AND round_number=? AND invocation_id=?',
                        (work_id,number,value['id'])).fetchone()
                    previous=json.loads(old['snapshot']) if old else None
                    check(previous is None or previous['version']<=value['version'],'STALE_APPROVAL_OBSERVATION')
                    if previous!=value:
                        self.work.event(work_id,'approval_observed',dict(source_round=number,snapshot=value))
                    self.db.execute('INSERT OR REPLACE INTO work_approvals VALUES (?,?,?,?,?)',
                        (work_id,number,value['id'],encoded(value),observed))
                observation=dict(run_id=run_id,observed_at=observed,state='observed',partial=bool(page.get('at_limit')),
                                 invocation_id=invocation_id)
                self.db.execute('INSERT OR REPLACE INTO work_approval_sync VALUES (?,?,?)',(work_id,number,encoded(observation)))
        except Exception:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO work_approval_sync VALUES (?,?,?)',
                    (work_id,number,encoded(dict(run_id=run_id,attempted_at=observed,state='unavailable'))))
            raise
        return self.history(work_id)
