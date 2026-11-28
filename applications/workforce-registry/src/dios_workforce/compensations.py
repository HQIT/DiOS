"""Explicit application-defined compensating work with independent authority and review."""
import json

from .registry import check, digest, encoded, identifier


class Compensations:
    def __init__(self, work):
        self.work, self.db = work, work.db

    async def settled(self, source, reviewer):
        original=await reviewer.get(source['run_id'])
        check(original['id']==source['run_id'] and original['state'] in ('succeeded','failed','cancelled'),'SOURCE_NOT_SETTLED')
        # Runtime success already requires settled tool calls. Failure/cancellation
        # can retain an uncertain dispatched effect; require a complete live view.
        if original['state']!='succeeded':
            page=await reviewer.tools(source['run_id'])
            check(isinstance(page.get('items'),list) and not page.get('at_limit')
                  and not page.get('migration_required'),'SOURCE_TOOL_OBSERVATION_INCOMPLETE')
            check(all(item['run_id']==source['run_id'] and item['state'] in
                      ('awaiting_approval','prepared','succeeded','failed','denied','cancelled')
                      for item in page['items']),'SOURCE_TOOL_RECONCILIATION_REQUIRED')

    def history(self, source_id):
        values=[]
        for row in self.db.execute('SELECT * FROM work_compensations WHERE source_id=? ORDER BY source_round',(source_id,)):
            value=dict(row)
            for field in ('request','source_snapshot','decision'):
                value[field]=json.loads(value[field]) if value[field] is not None else None
            value['work_item']=self.work.history(value['compensation_id'])
            values.append(value)
        inverse=[dict(r) for r in self.db.execute(
            'SELECT source_id,source_round FROM work_compensations WHERE compensation_id=?',(source_id,))]
        return dict(compensations=values,compensates=inverse)

    async def plan(self, source_id, compensation_id, reviewer, reason, key, *, expected_version):
        identifier(compensation_id);identifier(key)
        check(isinstance(reason,str) and 0<len(reason.strip())<=4000,'COMPENSATION_REASON_REQUIRED')
        source=self.work.get(source_id);await self.work.reviewer(source,reviewer)
        request=encoded(dict(compensation_id=compensation_id,reason=reason,key=key,expected_version=expected_version))
        old=self.db.execute('SELECT * FROM work_compensations WHERE source_id=? AND source_round=?',
                            (source_id,source['round_number'])).fetchone()
        if old:
            check(old['request']==request,'COMPENSATION_CONFLICT');return self.work.history(source_id)
        check(source['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(source_id!=compensation_id and not self.db.execute(
            'SELECT 1 FROM work_compensations WHERE compensation_id=? OR source_id=? OR compensation_id=?',
            (source_id,compensation_id,compensation_id)).fetchone(),'COMPENSATION_ALREADY_ASSIGNED')
        check(source['state'] in ('execution_failed','needs_revision','confirmed','awaiting_confirmation','human_owned')
              and source['run_id'],'SOURCE_NOT_SETTLED')
        parent=self.db.execute('SELECT parent_id FROM work_children WHERE child_id=?',(source_id,)).fetchone()
        if parent:
            check(self.work.get(parent['parent_id'])['state'] in ('coordinating','awaiting_group_review'),
                  'PARENT_REVIEW_ALREADY_FINAL')
        await self.settled(source,reviewer)
        target=self.work.get(compensation_id)
        check(target['state']=='bound' and target['round_number']==1,'COMPENSATION_NOT_READY')
        check(not self.db.execute('SELECT 1 FROM work_children WHERE child_id=?',(compensation_id,)).fetchone(),
              'COMPENSATION_ALREADY_ASSIGNED')
        actor=self.work.registry.get(target['employee'])
        check(actor['state']=='active' and actor['active']==target['employee_revision'],'EMPLOYEE_PUBLICATION_CHANGED')
        identity=json.loads(actor['identity'])
        check((identity['issuer'],identity['subject'])!=(source['reviewer']['issuer'],source['reviewer']['subject']),
              'SELF_REVIEW_DENIED')
        with self.db:
            check(self.work.get(source_id)['version']==source['version'] and
                  self.work.get(compensation_id)['version']==target['version'],'WORK_ITEM_VERSION_CONFLICT')
            self.db.execute('INSERT INTO work_compensations VALUES (?,?,?,?,?,NULL)',
                (source_id,source['round_number'],compensation_id,request,encoded(source)))
            self.db.execute("UPDATE work_items SET state='compensation_pending',version=version+1 WHERE id=?",(source_id,))
            self.work.event(source_id,'compensation_planned',dict(json.loads(request),source_run_id=source['run_id'],
                source_round=source['round_number'],operator=source['reviewer']))
            self.work.event(compensation_id,'compensation_assigned',dict(source_id=source_id,source_round=source['round_number'],source_run_id=source['run_id']))
        return self.work.history(source_id)

    async def confirm(self, source_id, reviewer, note, *, expected_version):
        check(isinstance(note,str) and 0<len(note.strip())<=4000,'REVIEW_NOTE_REQUIRED')
        source=self.work.get(source_id);await self.work.reviewer(source,reviewer)
        link=self.db.execute('SELECT * FROM work_compensations WHERE source_id=? AND source_round=?',
                            (source_id,source['round_number'])).fetchone()
        check(link is not None,'COMPENSATION_NOT_PLANNED')
        if link['decision'] is not None:
            old=json.loads(link['decision'])
            check(old['note']==note and source['version'] in (expected_version,expected_version+1),'DECISION_CONFLICT')
            return self.work.history(source_id)
        check(source['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(source['state']=='compensation_pending','COMPENSATION_NOT_PENDING')
        await self.settled(source,reviewer)
        target=self.work.get(link['compensation_id'])
        check(target['state']=='confirmed' and target['decision']['decision']=='accept','COMPENSATION_NOT_CONFIRMED')
        _,evidence=await self.work.evidence(target,reviewer)
        check(evidence is not None and evidence==target['evidence'],'COMPENSATION_EVIDENCE_CHANGED')
        decision=dict(note=note,reviewer=source['reviewer'],compensation_round=target['round_number'],
            compensation_run_id=target['run_id'],evidence=evidence)
        with self.db:
            check(self.work.get(source_id)['version']==source['version'] and
                  self.work.get(target['id'])['version']==target['version'],'WORK_ITEM_VERSION_CONFLICT')
            self.db.execute('UPDATE work_compensations SET decision=? WHERE source_id=? AND source_round=?',
                (encoded(decision),source_id,source['round_number']))
            self.db.execute("UPDATE work_items SET state='compensated',version=version+1 WHERE id=?",(source_id,))
            self.work.event(source_id,'compensation_confirmed',dict(decision,evidence_digest=digest(evidence)))
        return self.work.history(source_id)
