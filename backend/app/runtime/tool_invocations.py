"""Durable E2AG action gate. Only dispatch() may call the registered transport."""
from .work_policy import policy_matches
from .principals import admit
import asyncio
import json
from time import perf_counter
from uuid import uuid4
from sqlalchemy import select
from jsonschema import Draft202012Validator
from app.services.e2ag import Decision, evaluate_work_permission
from .database import RuntimeErrorCode
from .schema import attempts, artifacts, runs
from .store import bounded, digest, encoded, identifier
from .work_auth_schema import grants, work
from .work_authorizations import checked_work, require_current
from .tool_schema import invocations as calls, evidence, require, check
from .tool_receipts import ToolReceipts, durable_io, MAX_OUTPUT
from .lifecycle import admission


async def audit(c,scope,call_id,stage,outcome,reason,now,details=None):
    await c.execute(evidence.insert().values(id=str(uuid4()),scope=scope,invocation_id=call_id,stage=stage,
        decision=Decision('tool-'+stage,outcome,(reason,),evidence=details).as_dict(),created_at=now))


class ToolInvocations:
    def __init__(self,store,policy,catalog,provider,transport,approvers=None,secrets=None,identity_guard=None,policy_evaluator=None):
        from .storage_lease import bind_storage
        bind_storage(provider,store.db)
        self.store,self.policy,self.catalog,self.provider=store,policy,catalog,provider
        self.transport,self.approvers,self.secrets=transport,approvers,secrets
        from .policy_evaluator import OpaEvaluator
        self.policy_evaluator=policy_evaluator or OpaEvaluator()
        self.identity_guard=identity_guard;self.sender_evidence=None
        self.receipts=ToolReceipts(provider,store.scope);self._receipt_cursor=None

    async def _row(self,c,run_id,call_id):
        await require(c)
        row=(await c.execute(select(calls).where(calls.c.scope==self.store.scope,calls.c.run_id==run_id,calls.c.id==call_id))).mappings().first()
        if not row:raise RuntimeErrorCode('NOT_FOUND')
        return dict(row)

    async def _configuration(self,run_id,tool_id):
        run=await self.store.get(run_id)
        if not run['input'].get('work_authorization_ref'):raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
        try:
            current=await asyncio.to_thread(admit,self.policy,run['input']['runtime_profile'],
                run['input']['profile_digest'],run['input'].get('secret_refs',[]),run['input'].get('principal'))
        except RuntimeErrorCode as exc:
            from .work_authorizations import observe_identity_revocation
            await observe_identity_revocation(self.store,run,exc.code)
            raise
        registration=await asyncio.to_thread(self.catalog.registration,tool_id)
        return current,registration

    async def _binding(self,c,run_id,attempt_id,session_id,current,registration,now):
        if self.identity_guard:await self.identity_guard(c)
        run=await self.store._run(c,run_id)
        a=(await c.execute(select(attempts).where(attempts.c.id==attempt_id,attempts.c.run_id==run_id))).mappings().first()
        if not a:raise RuntimeErrorCode('STALE_OWNER')
        await self.store._owned(c,run_id,a['id'],a['worker_id'],a['worker_instance'],a['epoch'],authorize=True)
        if not session_id or a['session_id']!=session_id:raise RuntimeErrorCode('SESSION_CHANGED')
        work=await checked_work(c,run,a,now,'execute')
        if work is None:raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
        if not policy_matches(current,work['policy_digest']):raise RuntimeErrorCode('WORK_POLICY_CHANGED')
        await require_current(c,run,a,now,session_id=session_id)
        decision=evaluate_work_permission(work['permissions'],registration['permission'])
        if not decision.permits_execution:raise RuntimeErrorCode(decision.reason_codes[0])
        from .delegations import check_work
        await check_work(c,self.store.db,run,work,registration['permission'])
        credential=registration.get('credential_ref')
        if credential is not None and credential not in work['secret_refs']:raise RuntimeErrorCode('SECRET_ACCESS_DENIED')
        grant=(await c.execute(select(grants).where(grants.c.attempt_id==attempt_id))).mappings().one()
        # The final tool-specific delegation PDP may outlive the local execution
        # authority. Recheck deadlines after that await without another PDP call.
        now=await self.store.db.now(c)
        if a['lease_until']<=now:raise RuntimeErrorCode('STALE_OWNER')
        if work['expires_at']<=now:raise RuntimeErrorCode('WORK_AUTHORIZATION_EXPIRED')
        if grant['expires_at']<=now:raise RuntimeErrorCode('EXECUTION_GRANT_EXPIRED')
        binding=dict(run_id=run_id,attempt_id=attempt_id,session_id=session_id,worker_id=a['worker_id'],
            worker_instance=a['worker_instance'],lease_epoch=a['epoch'],authorization_epoch=a['authorization_epoch'],
            authorization_ref=work['id'],policy_digest=work['policy_digest'],permissions_digest=digest(work['permissions']),
            subject=work['subject'],actor=work['actor'])
        return binding,min(now+600,grant['expires_at'],work['expires_at'])

    async def _evaluate_policy(self,c,call_id,binding,registration,arguments):
        configuration=registration.get('policy_evaluator')
        if configuration is None:return
        authorization=(await c.execute(select(work).where(work.c.id==binding['authorization_ref'],
            work.c.scope==self.store.scope,work.c.run_id==binding['run_id']))).mappings().one()
        permission=registration['permission']
        value={'subject':{'type':'principal','id':binding['subject']},
            'action':{'name':permission['action'],'properties':{'arguments':arguments}},
            'resource':{'type':permission['resource_kind'],'id':permission['resource_id'],
                'properties':{'version':permission['resource_version']}},
            'context':{'scope':self.store.scope,'binding':binding,'permission':permission,'permissions':authorization['permissions']}}
        started=perf_counter()
        try:result=await self.policy_evaluator.evaluate(configuration,value)
        except RuntimeErrorCode as exc:
            elapsed=perf_counter()-started
            await audit(c,self.store.scope,call_id,'policy','deny',exc.code,await self.store.db.now(c),
                {'run_id':binding['run_id'],'attempt_id':binding['attempt_id'],
                 'timing':{'clock':'monotonic','opa_request_seconds':elapsed}})
            raise
        elapsed=perf_counter()-started
        result={**result,'run_id':binding['run_id'],'attempt_id':binding['attempt_id'],
            'timing':{'clock':'monotonic','opa_request_seconds':elapsed}}
        now=await self.store.db.now(c)
        await audit(c,self.store.scope,call_id,'policy','allow' if result['allow'] else 'deny',
            'POLICY_ALLOWED' if result['allow'] else 'POLICY_DENIED',now,result)
        if not result['allow']:raise RuntimeErrorCode('POLICY_DENIED')

    async def request(self,run_id,attempt_id,session_id,tool_id,arguments,key):
        identifier(key);identifier(tool_id);bounded(arguments)
        if not isinstance(arguments,dict):raise RuntimeErrorCode('INVALID_TOOL_ARGUMENTS')
        current,reg=await self._configuration(run_id,tool_id)
        if not Draft202012Validator(reg['input_schema']).is_valid(arguments):raise RuntimeErrorCode('INVALID_TOOL_ARGUMENTS')
        request_hash=digest([session_id,tool_id,arguments]);failure=None;call_id=str(uuid4())
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c);now=await self.store.db.now(c)
            old=(await c.execute(select(calls).where(calls.c.scope==self.store.scope,calls.c.run_id==run_id,
                calls.c.attempt_id==attempt_id,calls.c.request_key==key))).mappings().first()
            if old:
                if old['request_digest']!=request_hash:raise RuntimeErrorCode('IDEMPOTENCY_CONFLICT')
                return self.public(old,now)
            try:
                binding,deadline=await self._binding(c,run_id,attempt_id,session_id,current,reg,now)
                if reg.get('policy_evaluator'):
                    await self._evaluate_policy(c,call_id,binding,reg,arguments)
                    binding,deadline=await self._binding(c,run_id,attempt_id,session_id,current,reg,await self.store.db.now(c))
                state='awaiting_approval' if reg['approval_required'] else 'prepared'
                await c.execute(calls.insert().values(id=call_id,scope=self.store.scope,run_id=run_id,attempt_id=attempt_id,
                    request_key=key,request_digest=request_hash,tool_id=tool_id,registration_digest=reg['digest'],binding=binding,
                    permission=reg['permission'],arguments=arguments,arguments_digest=digest(arguments),state=state,version=1,
                    approval_state='pending' if reg['approval_required'] else 'not_required',expires_at=deadline,
                    created_at=now,updated_at=now))
                await audit(c,self.store.scope,call_id,'request','approval_required' if reg['approval_required'] else 'allow',
                    'TOOL_APPROVAL_REQUIRED' if reg['approval_required'] else 'TOOL_PREPARED',now,{'run_id':run_id,'arguments_digest':digest(arguments)})
            except RuntimeErrorCode as exc:
                failure=exc
                await audit(c,self.store.scope,call_id,'request','deny',exc.code,now,{'run_id':run_id,'attempt_id':attempt_id,'tool_id':tool_id})
        if failure:raise failure
        return await self.get(run_id,call_id)

    async def decide(self,run_id,call_id,version,decision,token,reason='',*,required_subject=None,access_guard=None):
        if decision not in {'approved','denied'} or len(reason)>2000:raise RuntimeErrorCode('INVALID_APPROVAL_DECISION')
        row=await self.get(run_id,call_id)
        if self.approvers is None:raise RuntimeErrorCode('APPROVER_PROVIDER_REQUIRED')
        actor=await asyncio.to_thread(self.approvers.authenticate,token,self.store.scope,row['tool_id'])
        if required_subject is not None and actor!=required_subject:raise RuntimeErrorCode('AUTH_APPROVER_MISMATCH')
        current,reg=await self._configuration(run_id,row['tool_id'])
        failure=None
        async with self.store.db.transaction(self.store.scope) as c:
            item=await self._row(c,run_id,call_id);now=await self.store.db.now(c)
            try:
                if access_guard:await asyncio.to_thread(access_guard,await self.store._run(c,run_id))
                if item['version']!=version:raise RuntimeErrorCode('VERSION_CONFLICT')
                if item['state']!='awaiting_approval' or item['approval_state']!='pending':raise RuntimeErrorCode('APPROVAL_ALREADY_DECIDED')
                if self.approvers.authenticate(token,self.store.scope,item['tool_id'])!=actor:raise RuntimeErrorCode('APPROVER_FORBIDDEN')
                if actor in {item['binding']['subject'],item['binding']['actor']}:raise RuntimeErrorCode('APPROVER_MUST_BE_INDEPENDENT')
                await self._recheck(c,item,current,reg,now)
                # External policy evaluation can wait while operator eligibility changes.
                if access_guard:await asyncio.to_thread(access_guard,await self.store._run(c,run_id))
                if self.approvers.authenticate(token,self.store.scope,item['tool_id'])!=actor:raise RuntimeErrorCode('APPROVER_FORBIDDEN')
                await c.execute(calls.update().where(calls.c.id==call_id).values(state='prepared' if decision=='approved' else 'denied',
                    approval_state=decision,approver=actor,approval_reason=reason,version=version+1,updated_at=now))
                await audit(c,self.store.scope,call_id,'approval','allow' if decision=='approved' else 'deny',
                    'TOOL_APPROVED' if decision=='approved' else 'TOOL_DENIED',now,{'approver':actor})
            except RuntimeErrorCode as exc:
                failure=exc;await audit(c,self.store.scope,call_id,'approval','deny',exc.code,now)
        if failure:raise failure
        return await self.get(run_id,call_id)

    async def _approval_eligible(self,c,item,reg,now):
        if reg['approval_required'] and item['approval_state']=='approved' and not item['approver']:
            raise RuntimeErrorCode('TOOL_APPROVAL_REQUIRED')
        if reg['approval_required'] and item['approver']:
            if self.approvers is None:raise RuntimeErrorCode('APPROVER_PROVIDER_REQUIRED')
            try:self.approvers.eligible(item['approver'],self.store.scope,item['tool_id'])
            except RuntimeErrorCode as exc:
                # Definitive revocation invalidates the old approval permanently.
                # Unavailable/malformed providers retain the approval for revalidation.
                if exc.code=='APPROVER_FORBIDDEN' and item['state']=='prepared' and item['approval_state']=='approved':
                    await c.execute(calls.update().where(calls.c.id==item['id'],calls.c.version==item['version'])
                        .values(state='denied',approval_state='invalidated',reason='TOOL_APPROVAL_INVALIDATED',
                            version=item['version']+1,updated_at=now))
                    await audit(c,self.store.scope,item['id'],'approval','deny','TOOL_APPROVAL_INVALIDATED',now,
                        {'approver':item['approver'],'cause':exc.code})
                raise

    async def _recheck(self,c,item,current,reg,now):
        await self._approval_eligible(c,item,reg,now)
        if item['expires_at']<=now:raise RuntimeErrorCode('TOOL_AUTHORIZATION_EXPIRED')
        if reg['digest']!=item['registration_digest']:raise RuntimeErrorCode('TOOL_REGISTRATION_CHANGED')
        binding,_=await self._binding(c,item['run_id'],item['attempt_id'],item['binding']['session_id'],current,reg,now)
        if binding!=item['binding']:raise RuntimeErrorCode('TOOL_BINDING_CHANGED')
        if digest(item['arguments'])!=item['arguments_digest']:raise RuntimeErrorCode('TOOL_ARGUMENTS_CHANGED')
        if reg.get('policy_evaluator'):
            await self._evaluate_policy(c,item['id'],binding,reg,item['arguments'])
            now=await self.store.db.now(c)
            if item['expires_at']<=now:raise RuntimeErrorCode('TOOL_AUTHORIZATION_EXPIRED')
            await self._binding(c,item['run_id'],item['attempt_id'],item['binding']['session_id'],current,reg,now)
            await self._approval_eligible(c,item,reg,await self.store.db.now(c))
        # Work binding itself may now await the delegation PDP.
        now=await self.store.db.now(c)
        if item['expires_at']<=now:raise RuntimeErrorCode('TOOL_AUTHORIZATION_EXPIRED')
        await self._approval_eligible(c,item,reg,now)

    async def dispatch(self,run_id,call_id,version):
        row=await self.get(run_id,call_id)
        if row['approval_state']=='invalidated':raise RuntimeErrorCode('TOOL_APPROVAL_INVALIDATED')
        if row['state'] in {'dispatched','result_unknown','succeeded','failed'}:return row
        current,reg=await self._configuration(run_id,row['tool_id'])
        failure=None
        async with self.store.db.transaction(self.store.scope) as c:
            item=await self._row(c,run_id,call_id);now=await self.store.db.now(c)
            if item['state'] in {'dispatched','result_unknown','succeeded','failed'}:return self.public(item,now)
            try:
                if item['version']!=version:raise RuntimeErrorCode('VERSION_CONFLICT')
                if item['state']!='prepared':raise RuntimeErrorCode('TOOL_APPROVAL_REQUIRED')
                await self._recheck(c,item,current,reg,now)
            except RuntimeErrorCode as exc:
                failure=exc;await audit(c,self.store.scope,call_id,'preflight','deny',exc.code,now)
        if failure:raise failure
        headers={}
        if reg.get('credential_ref'):
            run=await self.store.get(run_id);request=reg['credential_ref']
            if request not in run['input'].get('secret_refs',[]) or self.secrets is None:raise RuntimeErrorCode('SECRET_ACCESS_DENIED')
            mounts=await asyncio.to_thread(self.secrets.resolve,run['input']['runtime_profile'],[request])
            from pathlib import Path
            token=await asyncio.to_thread(Path(mounts[0].source).read_text,encoding='utf-8')
            token=token.strip()
            if not token or '\r' in token or '\n' in token:raise RuntimeErrorCode('SECRET_CONTENT_INVALID')
            headers['Authorization']='Bearer '+token
        # Initialization has no business tool call. The bound gate runs immediately before tools/call.
        async with self.transport.open(reg,headers) as channel:
            # Re-read policies after initialization, before the transaction that commits dispatch.
            current,fresh=await self._configuration(run_id,row['tool_id'])
            if fresh['digest']!=reg['digest']:raise RuntimeErrorCode('TOOL_REGISTRATION_CHANGED')
            failure=None;claimed=False
            async with self.store.db.transaction(self.store.scope) as c:
                item=await self._row(c,run_id,call_id);now=await self.store.db.now(c)
                if item['state'] in {'dispatched','result_unknown','succeeded','failed'}:return self.public(item,now)
                try:
                    if item['version']!=version:raise RuntimeErrorCode('VERSION_CONFLICT')
                    if item['state']!='prepared':raise RuntimeErrorCode('TOOL_APPROVAL_REQUIRED')
                    await self._recheck(c,item,current,reg,now)
                    if reg['approval_required'] and item['approval_state']!='approved':raise RuntimeErrorCode('TOOL_APPROVAL_REQUIRED')
                    from .tool_budgets import require_dispatch
                    budget=await require_dispatch(c,self.store.scope,await self.store._run(c,run_id))
                    now=await self.store.db.now(c)
                    await c.execute(calls.update().where(calls.c.id==call_id).values(state='dispatched',
                        approval_state='consumed' if reg['approval_required'] else 'not_required',version=version+1,
                        updated_at=now,dispatch_deadline=now+60))
                    await audit(c,self.store.scope,call_id,'dispatch','allow','TOOL_DISPATCH_COMMITTED',now,
                        {'binding':item['binding'],'arguments_digest':item['arguments_digest'],'dispatch_version':version+1,'sender':self.sender_evidence,'budget':budget})
                    item=dict(item,state='dispatched',version=version+1,approval_state='consumed' if reg['approval_required'] else 'not_required')
                    claimed=True
                except RuntimeErrorCode as exc:
                    failure=exc;await audit(c,self.store.scope,call_id,'dispatch','deny',exc.code,now)
            if failure:raise failure
            if claimed:await self._execute_dispatched(item,reg,channel)
        return await self.get(run_id,call_id)

    async def _execute_dispatched(self,item,reg,channel):
        intent=None;failure='TOOL_OUTCOME_UNKNOWN';cancelled=None
        try:
            # The local record must be writable before the external tool is invoked.
            intent=await durable_io(self.receipts.prepare,item,reg)
            async with asyncio.timeout(60):
                result=await channel.call(reg['name'],item['arguments'],item['id'])
            raw=encoded(result)
            if len(raw)>MAX_OUTPUT:raise RuntimeErrorCode('TOOL_RESULT_TOO_LARGE')
            await durable_io(self.receipts.save,intent,raw,complete=True,reason='TOOL_RESPONSE_RECEIVED')
        except BaseException as exc:
            failure=exc.code if isinstance(exc,RuntimeErrorCode) else 'TOOL_OUTCOME_UNKNOWN'
            if intent is not None:
                try:
                    # A control-write failure or cancellation must never overwrite a full receipt.
                    if not await asyncio.to_thread(self.receipts.available,item['id']):
                        partial=getattr(exc,'partial',b'')[:MAX_OUTPUT]
                        await durable_io(self.receipts.save,intent,partial,complete=False,reason=failure)
                except Exception:pass
            if not isinstance(exc,Exception):cancelled=exc
        try:
            if await asyncio.to_thread(self.receipts.available,item['id']):
                await self.recover_receipt(item['run_id'],item['id'])
            else:
                await self._mark_unknown(item['run_id'],item['id'],failure)
        finally:
            if cancelled:raise cancelled

    async def _mark_unknown(self,run_id,call_id,reason):
        async with self.store.db.transaction(self.store.scope) as c:
            await admission(c,self.store.scope,ownership=True)
            item=await self._row(c,run_id,call_id);now=await self.store.db.now(c)
            if item['state']!='dispatched':return
            await c.execute(calls.update().where(calls.c.id==call_id).values(state='result_unknown',reason=reason,
                version=item['version']+1,updated_at=now))
            await audit(c,self.store.scope,call_id,'result','deny',reason,now)

    async def _receipt_binding(self,c,item,value):
        self.receipts.match(value,item)
        await self.store._run(c,item['run_id'])
        b=item['binding']
        a=(await c.execute(select(attempts).where(attempts.c.id==item['attempt_id'],attempts.c.run_id==item['run_id']))).mappings().first()
        if not a or (a['worker_id'],a['worker_instance'],a['epoch'],a['authorization_epoch'],a['session_id'])!=(
            b['worker_id'],b['worker_instance'],b['lease_epoch'],b['authorization_epoch'],b['session_id']):
            raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
        w=(await c.execute(select(work).where(work.c.id==b['authorization_ref'],work.c.scope==self.store.scope,work.c.run_id==item['run_id']))).mappings().first()
        if not w or w['policy_digest']!=b['policy_digest'] or digest(w['permissions'])!=b['permissions_digest'] or (w['subject'],w['actor'])!=(b['subject'],b['actor']):
            raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
        # Immutable original permission evidence, not a grant renewal or new execution permission.
        decision=evaluate_work_permission(w['permissions'],item['permission'])
        if not decision.permits_execution:raise RuntimeErrorCode(decision.reason_codes[0])
        grant=(await c.execute(select(grants).where(grants.c.attempt_id==a['id']))).mappings().first()
        expected=dict(authorization_id=b['authorization_ref'],run_id=item['run_id'],worker_id=b['worker_id'],worker_instance=b['worker_instance'],
            lease_epoch=b['lease_epoch'],authorization_epoch=b['authorization_epoch'],session_id=b['session_id'])
        if not grant or any(grant[k]!=v for k,v in expected.items()):raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
        rows=(await c.execute(select(evidence.c.decision).where(evidence.c.scope==self.store.scope,
            evidence.c.invocation_id==item['id'],evidence.c.stage=='dispatch'))).scalars().all()
        expected_version=value['intent']['dispatch_version']
        if not any(d.get('decision')=='allow' and d.get('evidence',{}).get('binding')==b
                and d.get('evidence',{}).get('arguments_digest')==item['arguments_digest']
                and d.get('evidence',{}).get('dispatch_version')==expected_version for d in rows):
            raise RuntimeErrorCode('TOOL_DISPATCH_EVIDENCE_REQUIRED')

    def _validated_receipt(self,call_id,captured):
        value=self.receipts.load(call_id)
        if value is None:raise RuntimeErrorCode('TOOL_RECEIPT_NOT_FOUND')
        # Anchor the frozen schema to the database before interpreting it.
        self.receipts.match(value,captured)
        receipt_blob=self.provider.verify(digest(value))
        result_blob=self.provider.verify(value['output']['key'])
        state,reason='result_unknown',value['reason']
        if value['kind']=='complete':
            try:
                result=json.loads(b''.join(self.provider.read(result_blob.key)).decode('utf-8'))
                schema=value['intent']['registration']['result_schema']
                valid=(isinstance(result,dict) and isinstance(result.get('content'),list)
                    and type(result.get('isError',False)) is bool and Draft202012Validator(schema).is_valid(result))
                if valid:
                    state,reason=('failed','TOOL_REPORTED_ERROR') if result.get('isError') else ('succeeded','TOOL_RESULT_VALIDATED')
                else:reason='TOOL_RESULT_INVALID'
            except (ValueError,UnicodeError,KeyError,TypeError):reason='TOOL_RESULT_INVALID'
        return value,receipt_blob,result_blob,state,reason

    async def recover_receipt(self,run_id,call_id,version=None):
        # Scope check precedes any local receipt read. No registration reload or upstream I/O.
        async with self.store.db.read(self.store.scope) as c:
            captured=await self._row(c,run_id,call_id)
        value,receipt_blob,result_blob,state,reason=await asyncio.to_thread(self._validated_receipt,call_id,captured)
        self.receipts.match(value,captured)
        failure=None
        async with self.store.db.transaction(self.store.scope) as c:
            await admission(c,self.store.scope,ownership=True)
            item=await self._row(c,run_id,call_id);now=await self.store.db.now(c)
            try:
                await self._receipt_binding(c,item,value)
                applied=(await c.execute(select(evidence.c.id).where(evidence.c.scope==self.store.scope,
                    evidence.c.invocation_id==call_id,evidence.c.stage=='receipt',
                    evidence.c.decision['evidence']['receipt_key'].as_string()==receipt_blob.key))).first()
                if applied:return self.public(item,now)
                if version is not None and item['version']!=version:raise RuntimeErrorCode('VERSION_CONFLICT')
                if item['version']!=captured['version']:raise RuntimeErrorCode('VERSION_CONFLICT')
                if item['state'] not in {'dispatched','result_unknown'}:raise RuntimeErrorCode('TOOL_RECEIPT_CONFLICT')
                for blob in (result_blob,receipt_blob):
                    old=(await c.execute(select(artifacts.c.id).where(artifacts.c.run_id==run_id,
                        artifacts.c.attempt_id==item['attempt_id'],artifacts.c.storage_key==blob.key))).first()
                    if not old:await c.execute(artifacts.insert().values(id=str(uuid4()),run_id=run_id,attempt_id=item['attempt_id'],
                        provider=self.provider.name,storage_key=blob.key,digest=blob.digest,size=blob.size,created_at=now))
                await c.execute(calls.update().where(calls.c.id==call_id).values(state=state,reason=reason,
                    result_key=result_blob.key,version=item['version']+1,updated_at=now))
                # Side-effect facts remain recordable after revocation; Run acceptance is separate.
                await audit(c,self.store.scope,call_id,'receipt','allow' if state!='result_unknown' else 'deny',reason,now,
                    {'receipt_key':receipt_blob.key,'result_key':result_blob.key,'binding':item['binding'],'execution_grant_renewed':False})
            except RuntimeErrorCode as exc:
                failure=exc;await audit(c,self.store.scope,call_id,'receipt-rejected','deny',exc.code,now)
        if failure:raise failure
        return await self.get(run_id,call_id)

    async def recover_pending(self,profile_id,limit=50):
        if type(limit) is not int or not 1<=limit<=100:raise RuntimeErrorCode('INVALID_LIMIT')
        # Repair historical terminal tasks as well as atomically closed new tasks.
        # Calls already dispatched retain their original receipt/reconciliation path.
        async with self.store.db.transaction(self.store.scope) as c:
            if not await check(c):return []
            terminal=(await c.execute(select(runs.c.id,runs.c.state).join(calls,calls.c.run_id==runs.c.id)
                .where(runs.c.scope==self.store.scope,calls.c.scope==self.store.scope,
                    runs.c.input['runtime_profile'].as_string()==profile_id,
                    runs.c.state.in_(['succeeded','failed','cancelled']),
                    calls.c.state.in_(['awaiting_approval','prepared']))
                .distinct().order_by(runs.c.id).limit(limit))).mappings().all()
            now=await self.store.db.now(c)
            for run in terminal:await close_unstarted(c,self.store.scope,run['id'],run['state'],now)
        async with self.store.db.read(self.store.scope) as c:
            if not await check(c):return []
            query=select(calls).join(runs,runs.c.id==calls.c.run_id).where(calls.c.scope==self.store.scope,
                runs.c.scope==self.store.scope,runs.c.input['runtime_profile'].as_string()==profile_id,
                calls.c.state.in_(['dispatched','result_unknown']))
            if self._receipt_cursor:query=query.where(calls.c.id>self._receipt_cursor)
            rows=(await c.execute(query.order_by(calls.c.id).limit(limit))).mappings().all()
            now=await self.store.db.now(c)
        self._receipt_cursor=rows[-1]['id'] if len(rows)==limit else None
        results=[]
        for row in rows:
            try:
                if await asyncio.to_thread(self.receipts.available,row['id']):
                    result=await self.recover_receipt(row['run_id'],row['id'],row['version'])
                elif row['state']=='dispatched' and row['dispatch_deadline']<=now:
                    result=await self.reconcile_overdue(row['run_id'],row['id'],row['version'])
                else:continue
                results.append({'id':row['id'],'state':result['state']})
            except (RuntimeErrorCode,OSError) as exc:results.append({'id':row['id'],'error':getattr(exc,'code','TOOL_RECEIPT_UNAVAILABLE')})
        return results

    @staticmethod
    def public(row,now):
        value={k:row[k] for k in ('id','run_id','attempt_id','tool_id','permission','arguments','arguments_digest','state','version',
            'approval_state','approver','approval_reason','expires_at','created_at','updated_at','result_key','reason')}
        value['session_id']=row['binding']['session_id']
        value['expired']=row['expires_at']<=now
        value['dispatch_overdue']=row['state']=='dispatched' and row['dispatch_deadline']<=now
        return value

    async def get(self,run_id,call_id):
        async with self.store.db.read(self.store.scope) as c:
            row=await self._row(c,run_id,call_id);now=await self.store.db.now(c)
            value=self.public(row,now)
            events=(await c.execute(select(evidence).where(evidence.c.scope==self.store.scope,evidence.c.invocation_id==call_id)
                .order_by(evidence.c.created_at.desc(),evidence.c.id).limit(30))).mappings().all()
            value['evidence']=[dict(stage=e['stage'],created_at=e['created_at'],decision=e['decision']) for e in events]
        value['receipt_available']=await asyncio.to_thread(self.receipts.available,call_id)
        return value

    async def list(self,run_id,limit=50):
        if type(limit) is not int or not 1<=limit<=100:raise RuntimeErrorCode('INVALID_LIMIT')
        async with self.store.db.read(self.store.scope) as c:
            await self.store._run(c,run_id)
            if not await check(c):return {'items':[],'migration_required':True,'at_limit':False}
            now=await self.store.db.now(c)
            rows=(await c.execute(select(calls).where(calls.c.scope==self.store.scope,calls.c.run_id==run_id)
                .order_by(calls.c.created_at.desc(),calls.c.id).limit(limit+1))).mappings().all()
        items=[self.public(x,now) for x in rows[:limit]]
        available=await asyncio.to_thread(lambda:[self.receipts.available(x['id']) for x in items])
        for item,present in zip(items,available):item['receipt_available']=present
        return {'items':items,'at_limit':len(rows)>limit,'observed_at':now}

    async def reconcile_overdue(self,run_id,call_id,version):
        async with self.store.db.transaction(self.store.scope) as c:
            item=await self._row(c,run_id,call_id);now=await self.store.db.now(c)
            if item['version']!=version:raise RuntimeErrorCode('VERSION_CONFLICT')
            await admission(c,self.store.scope,ownership=True)
            if item['state']!='dispatched' or item['dispatch_deadline']>now:raise RuntimeErrorCode('TOOL_OBSERVATION_PENDING')
            await c.execute(calls.update().where(calls.c.id==call_id).values(state='result_unknown',reason='TOOL_DISPATCH_OBSERVATION_EXPIRED',
                version=version+1,updated_at=now))
            await audit(c,self.store.scope,call_id,'reconcile','deny','TOOL_DISPATCH_OBSERVATION_EXPIRED',now)
        return await self.get(run_id,call_id)


async def close_unstarted(c,scope,run_id,run_state,now):
    """Close only calls with durable evidence that dispatch was never committed."""
    if run_state not in {'succeeded','failed','cancelled'} or not await check(c):return
    rows=(await c.execute(select(calls).where(calls.c.scope==scope,calls.c.run_id==run_id,
        calls.c.state.in_(['awaiting_approval','prepared'])))).mappings().all()
    for row in rows:
        reason='TOOL_NOT_DISPATCHED_RUN_TERMINAL'
        await c.execute(calls.update().where(calls.c.id==row['id'],calls.c.version==row['version'])
            .values(state='failed',reason=reason,version=row['version']+1,updated_at=now))
        # Original approval, approver and reason remain historical evidence.
        await audit(c,scope,row['id'],'lifecycle','deny',reason,now,
            {'run_state':run_state,'previous_state':row['state'],'approval_state':row['approval_state']})


async def require_settled(c,scope,run_id):
    if not await check(c):return
    unresolved=(await c.execute(select(calls.c.id).where(calls.c.scope==scope,calls.c.run_id==run_id,
        calls.c.state.in_(['dispatched','result_unknown'])).limit(1))).first()
    if unresolved:raise RuntimeErrorCode('TOOL_RECONCILIATION_REQUIRED')
