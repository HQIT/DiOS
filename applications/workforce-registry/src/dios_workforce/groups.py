"""Fixed one-level business decomposition and explicit parent evidence review."""
import json

from .registry import check, digest, encoded, identifier, principal


class WorkGroups:
    def __init__(self, work):
        self.work,self.registry,self.db=work,work.registry,work.db

    def definition(self, parent):
        row=self.db.execute('SELECT definition FROM work_groups WHERE parent_id=?',(parent,)).fetchone()
        check(row is not None,'WORK_GROUP_NOT_FOUND')
        return json.loads(row[0]) if row[0] is not None else None

    async def create(self, parent, title, goal, reviewer):
        identifier(parent)
        check(isinstance(title,str) and 0<len(title)<=500,'INVALID_TITLE')
        check(isinstance(goal,str) and 0<len(goal.strip())<=8000,'INVALID_GOAL')
        identity=principal(await reviewer.identity());spec={'title':title,'goal':goal}
        if self.db.execute('SELECT id FROM work_items WHERE id=?',(parent,)).fetchone():
            self.definition(parent);row=self.work.get(parent)
            check(row['specification']==spec and row['reviewer']==identity,'WORK_ITEM_IDEMPOTENCY_CONFLICT')
            return row
        with self.db:
            self.db.execute("INSERT INTO work_items(id,specification,reviewer,state,version) VALUES (?,?,?,'group_draft',1)",
                            (parent,encoded(spec),encoded(identity)))
            self.db.execute('INSERT INTO work_groups VALUES (?,NULL)',(parent,))
            self.work.event(parent,'group_created',{'reviewer':identity})
        return self.work.get(parent)

    async def split(self, parent, children, reviewer, *, expected_version):
        check(isinstance(children,list) and 2<=len(children)<=16 and len(set(children))==len(children),'INVALID_CHILD_SET')
        for child in children:identifier(child)
        row=self.work.get(parent);definition=self.definition(parent)
        await self.work.reviewer(row,reviewer)
        if definition is not None:
            check(definition['children']==children and definition['expected_version']==expected_version,'SPLIT_CONFLICT')
            return self.work.history(parent)
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state']=='group_draft','GROUP_ALREADY_STARTED')
        assignments=[];employees=set()
        for child_id in children:
            child=self.work.get(child_id)
            check(child['state']=='bound' and child['round_number']==1,'CHILD_NOT_READY_FOR_SPLIT')
            check(not self.db.execute('SELECT 1 FROM work_children WHERE child_id=?',(child_id,)).fetchone(),'CHILD_ALREADY_ASSIGNED')
            check(not self.db.execute('SELECT 1 FROM work_compensations WHERE compensation_id=?',(child_id,)).fetchone(),'COMPENSATION_ALREADY_ASSIGNED')
            actor=self.registry.get(child['employee'])
            check(actor['state']=='active' and actor['active']==child['employee_revision'],'EMPLOYEE_PUBLICATION_CHANGED')
            identity=json.loads(actor['identity'])
            check((identity['issuer'],identity['subject'])!=(row['reviewer']['issuer'],row['reviewer']['subject']),'SELF_REVIEW_DENIED')
            check(child['employee'] not in employees,'DISTINCT_EMPLOYEES_REQUIRED');employees.add(child['employee'])
            assignments.append({'child_id':child_id,'employee':child['employee'],'initial_revision':child['employee_revision'],
                'role':self.registry.revision(child['employee'],child['employee_revision'])['role']})
        definition={'children':children,'assignments':assignments,'expected_version':expected_version}
        with self.db:
            for position,child_id in enumerate(children):
                self.db.execute('INSERT INTO work_children VALUES (?,?,?)',(parent,child_id,position))
                self.work.event(child_id,'assigned_to_parent',{'parent_id':parent})
            self.db.execute('UPDATE work_groups SET definition=? WHERE parent_id=?',(encoded(definition),parent))
            self.db.execute("UPDATE work_items SET state='coordinating',version=version+1 WHERE id=?",(parent,))
            self.work.event(parent,'split',definition)
        return self.work.history(parent)

    async def evidence(self, parent, reviewer):
        definition=self.definition(parent);check(definition is not None,'GROUP_NOT_SPLIT')
        collected=[]
        for assignment in definition['assignments']:
            child=self.work.get(assignment['child_id'])
            check(child['employee']==assignment['employee'],'SPLIT_ASSIGNMENT_CHANGED')
            check(child['state']=='confirmed' and child['decision']['decision']=='accept','CHILD_WORK_NOT_CONFIRMED')
            _,evidence=await self.work.evidence(child,reviewer)
            check(evidence is not None and evidence==child['evidence'],'CHILD_EVIDENCE_CHANGED')
            collected.append({k:child[k] for k in ('id','round_number','version','employee','employee_revision',
                                                  'run_id','evidence','decision')})
        return {'plan_digest':digest(definition),'children':collected}

    async def collect(self, parent, reviewer, *, expected_version):
        row=self.work.get(parent)
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state'] in ('coordinating','awaiting_group_review'),'GROUP_NOT_COLLECTABLE')
        await self.work.reviewer(row,reviewer)
        evidence=await self.evidence(parent,reviewer)
        if row['state']=='coordinating' or row['evidence']!=evidence:
            with self.db:
                self.db.execute("UPDATE work_items SET state='awaiting_group_review',evidence=?,version=version+1 WHERE id=?",(encoded(evidence),parent))
                self.work.event(parent,'group_collected',{'evidence_digest':digest(evidence),'children':[c['id'] for c in evidence['children']]})
        return self.work.get(parent)

    async def confirm(self, parent, reviewer, decision, note, *, expected_version):
        check(decision in ('accept','reject'),'INVALID_DECISION')
        check(isinstance(note,str) and 0<len(note.strip())<=4000,'REVIEW_NOTE_REQUIRED')
        self.definition(parent);row=self.work.get(parent)
        await self.work.reviewer(row,reviewer)
        value={'decision':decision,'note':note,'reviewer':row['reviewer']}
        if row['decision'] is not None:
            check(row['decision']==value and row['version'] in (expected_version,expected_version+1),'DECISION_CONFLICT')
            return row
        check(row['version']==expected_version,'WORK_ITEM_VERSION_CONFLICT')
        check(row['state']=='awaiting_group_review','GROUP_NOT_READY_FOR_REVIEW')
        evidence=await self.evidence(parent,reviewer)
        check(evidence==row['evidence'],'GROUP_EVIDENCE_CHANGED')
        with self.db:
            self.db.execute('UPDATE work_items SET state=?,decision=?,version=version+1 WHERE id=?',
                ('group_confirmed' if decision=='accept' else 'group_needs_revision',encoded(value),parent))
            self.work.event(parent,'group_human_decision',dict(value,evidence_digest=digest(evidence)))
        return self.work.get(parent)
