"""Opt-in trusted-operator task API; never starts a Worker or migrates a database."""
import asyncio
from contextlib import asynccontextmanager
import secrets
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError
from app.config import settings
from app.runtime.database import Database, RuntimeErrorCode
from app.services.task_console import TaskConsole


class RuntimeRoute(APIRoute):
    def get_route_handler(self):
        original=super().get_route_handler()
        async def handler(request):
            try:
                from app.runtime.application_contracts import HEADER, API_VERSION
                requested=request.headers.get(HEADER)
                if requested is not None and requested != API_VERSION:
                    raise HTTPException(409,detail="RUNTIME_CONTRACT_UNSUPPORTED",headers={HEADER:API_VERSION})
                response=await original(request)
                response.headers[HEADER]=API_VERSION
                response.headers["Cache-Control"]="no-store"
                response.headers["X-Content-Type-Options"]="nosniff"
                return response
            except RuntimeErrorCode as exc:
                status=404 if exc.code in {'NOT_FOUND','SAMPLE_NOT_FOUND','PROFILE_NOT_AVAILABLE'} else 409
                if exc.code.startswith('INVALID_'):status=422
                if exc.code.startswith('AUTH_'):status=403
                if exc.code=='AUTH_UNAUTHENTICATED':status=401
                if exc.code=='AUTH_PROVIDER_UNAVAILABLE':status=503
                if exc.code=='KNOWLEDGE_PROVIDER_UNAVAILABLE':status=503
                if exc.code=='KNOWLEDGE_QUERY_INTERRUPTED':status=504
                if exc.code in {'PREVIEW_TOO_LARGE','PREVIEW_NOT_TEXT'}:status=413
                raise HTTPException(status,detail=exc.code,headers={'Cache-Control':'no-store'}) from None
            except (SQLAlchemyError,OSError,ValueError):
                raise HTTPException(503,detail='RUNTIME_UNAVAILABLE') from None
        return handler


async def operator(request: Request):
    if not settings.runtime_console_enabled:raise HTTPException(503,detail='RUNTIME_CONSOLE_DISABLED')
    auth=request.headers.get('authorization','')
    token=auth[7:].strip() if auth.lower().startswith('bearer ') else request.headers.get('x-dios-access-token','')
    if settings.access_token and secrets.compare_digest(token.encode(),settings.access_token.encode()):return None
    console=getattr(request.app.state,'runtime_console',None)
    if console:
        async with console.db.read(console.scope):pass
        provider=await asyncio.to_thread(console.identity_provider)
        if provider is not None:return await asyncio.to_thread(provider.authenticate,token,console.scope)
    if not settings.access_token:raise HTTPException(503,detail='RUNTIME_OPERATOR_TOKEN_REQUIRED')
    raise HTTPException(401,detail='Invalid or missing access token')


@asynccontextmanager
async def console_lifespan(app):
    db=None;app.state.runtime_console=None;schedule_loop=None;deployment_loop=None
    if (settings.runtime_console_enabled and settings.runtime_database_url
            and settings.runtime_profiles_dir and settings.runtime_artifacts_root):
        db=Database(settings.runtime_database_url)
        from app.runtime.work_policy import FileWorkPolicy
        policy=FileWorkPolicy(settings.runtime_policy_file) if settings.runtime_policy_file else None
        app.state.runtime_console=TaskConsole(db,settings.runtime_scope,
            settings.runtime_profiles_dir,settings.runtime_artifacts_root,policy=policy)
        if settings.runtime_tools_file and policy:
            from app.runtime.tool_invocations import ToolInvocations
            from app.runtime.tool_catalog import FileToolCatalog, FileApprovers
            from app.runtime.mcp_tool_transport import McpHttpTransport
            from app.runtime.secret_provider import FileSecretMounts
            console=app.state.runtime_console
            console.tools=ToolInvocations(console.store,policy,FileToolCatalog(settings.runtime_tools_file),console.provider,
                McpHttpTransport(),FileApprovers(settings.runtime_approvers_file) if settings.runtime_approvers_file else None,
                FileSecretMounts(settings.runtime_secrets_file,settings.runtime_scope) if settings.runtime_secrets_file else None)

    if app.state.runtime_console is not None:
        from app.services.run_schedule_worker import ScheduleLoop
        schedule_loop=ScheduleLoop(app.state.runtime_console,enabled=settings.runtime_schedules_enabled)
        schedule_loop.start()
        from app.services.deployment_worker import DeploymentLoop
        deployment_loop=DeploymentLoop(app.state.runtime_console)
        deployment_loop.start()
    try:yield
    finally:
        if schedule_loop:await schedule_loop.stop()
        if deployment_loop:await deployment_loop.stop()
        if db:await db.close()
        app.state.runtime_console=None


async def service(request:Request,_=Depends(operator)):
    value=getattr(request.app.state,'runtime_console',None)
    if value is None:raise HTTPException(503,detail='RUNTIME_CONFIGURATION_REQUIRED')
    bound=value.for_principal(_)
    async def recheck():
        current=await operator(request)
        if current!=_:raise RuntimeErrorCode('AUTH_PRINCIPAL_REVOKED')
    bound.credential_recheck=recheck
    return bound


router=APIRouter(prefix='/runtime',tags=['runtime-tasks'],route_class=RuntimeRoute)

_logout_slots=asyncio.Semaphore(4)

@router.post('/identity/logout/{provider_id}')
async def backchannel_logout(provider_id:str,request:Request):
    """Provider-signed endpoint; browser cookies and operator tokens grant no authority."""
    import os
    from urllib.parse import parse_qs
    from app.runtime.logout_verifier import verify
    from app.runtime.logout_events import receive
    console=getattr(request.app.state,'runtime_console',None)
    path=os.environ.get('DIOS_RUNTIME_LOGOUT_CONFIG');binary=os.environ.get('DIOS_RUNTIME_LOGOUT_VERIFIER')
    if console is None or not path or not binary:raise HTTPException(503,detail='LOGOUT_NOT_CONFIGURED')
    if _logout_slots.locked():raise HTTPException(503,detail='LOGOUT_BUSY')
    if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/x-www-form-urlencoded':raise HTTPException(415,detail='LOGOUT_FORM_REQUIRED')
    async with _logout_slots:
        try:
            raw=b''
            async with asyncio.timeout(3):
                async for chunk in request.stream():
                    raw+=chunk
                    if len(raw)>32768:raise HTTPException(413,detail='LOGOUT_BODY_TOO_LARGE')
            values=parse_qs(raw.decode('ascii'),strict_parsing=True,max_num_fields=1)
            if set(values)!={'logout_token'} or len(values['logout_token'])!=1 or not 1<=len(values['logout_token'][0])<=16384:raise ValueError()
        except (ValueError,UnicodeError,TimeoutError):raise HTTPException(400,detail='LOGOUT_TOKEN_INVALID') from None
        try:
            event,token_digest=await verify(path,binary,provider_id,values['logout_token'][0])
            await receive(console.store,event,token_digest)
        except RuntimeErrorCode as exc:
            status=400 if exc.code in {'LOGOUT_TOKEN_INVALID','LOGOUT_EVENT_CONFLICT'} else 503
            raise HTTPException(status,detail=exc.code) from None
    return Response(status_code=200,headers={'Cache-Control':'no-store'})

class TaskInput(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    profile_id: str=Field(min_length=1,max_length=160)
    sample_id: str|None=Field(default=None,min_length=1,max_length=160)
    input_payload: dict|None=None
    goal: str=Field(min_length=1,max_length=2000)
    idempotency_key: str=Field(min_length=1,max_length=160)

    @model_validator(mode='after')
    def exactly_one_input(self):
        if (self.sample_id is None)==(self.input_payload is None):
            raise ValueError('Supply exactly one of sample_id or input_payload')
        return self

class Submit(TaskInput):
    expected_profile_digest: str|None=Field(default=None,pattern=r'^[0-9a-f]{64}$')
    delegation_ref: str|None=Field(default=None,min_length=1,max_length=160)
    parent_run_id: str|None=Field(default=None,min_length=1,max_length=160)

class Cancel(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: int=Field(ge=0)

class DelegationPermission(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    action: str=Field(min_length=1,max_length=160)
    resource_kind: str=Field(min_length=1,max_length=160)
    resource_id: str=Field(min_length=1,max_length=160)
    resource_version: str=Field(min_length=1,max_length=160)
    constraint_revision: str=Field(min_length=1,max_length=160)

class DelegationIssue(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    idempotency_key: str=Field(min_length=1,max_length=160)
    parent_ref: str=Field(min_length=1,max_length=80)
    profile_id: str=Field(min_length=1,max_length=160)
    actor: str=Field(min_length=1,max_length=160)
    parent_run_id: str|None=Field(default=None,min_length=1,max_length=160)
    expires_at: float=Field(gt=0,allow_inf_nan=False)
    permissions: list[DelegationPermission]=Field(min_length=1,max_length=64)

async def delegation_service(s=Depends(service)):
    from app.services.delegation_access import AuthorizedDelegations
    return AuthorizedDelegations(s)

@router.post('/delegations',status_code=201)
async def delegation_issue(body:DelegationIssue,s=Depends(delegation_service)):
    return await s.issue(body.model_dump())

@router.get('/delegations/{reference}')
async def delegation_show(reference:str,s=Depends(delegation_service)):
    return await s.show(reference)

@router.post('/delegations/{reference}/revoke')
async def delegation_revoke(reference:str,body:Cancel,s=Depends(delegation_service)):
    return await s.revoke(reference,body.expected_version)

@router.get('/identity')
async def identity(s=Depends(service)):
    if s.principal is None:return {'mode':'operator','principal':None}
    row=await asyncio.to_thread(s.access_row)
    return {'mode':'principal','principal':s.principal,'profiles':{k:v['actions'] for k,v in row['profiles'].items()}}

@router.get('/profiles')
async def profiles(s=Depends(service)):
    return {'items':await s.catalog()}


class DeploymentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1,max_length=160)
    revision_ref: str | None = Field(default=None,min_length=1,max_length=160)
    idempotency_key: str = Field(min_length=1,max_length=160)
    expected_version: int = Field(ge=0)
    probe_input: dict
    timeout: int = Field(default=300,ge=10,le=3600)
    rollback: bool = False


def deployment_manager(s):
    if s.principal is not None:raise HTTPException(403,detail='AUTH_ACTION_DENIED')
    from app.runtime.deployments import Deployments
    from app.runtime.tool_catalog import FileApprovers
    approvers=FileApprovers(settings.runtime_approvers_file) if settings.runtime_approvers_file else None
    return Deployments(s.db,s.scope,s.policy,approvers)


def compatibility_manager(s):
    if s.principal is not None:raise HTTPException(403,detail='AUTH_ACTION_DENIED')
    from app.runtime.revision_compatibility import RevisionCompatibility
    from app.runtime.secret_provider import FileSecretMounts
    resolver=FileSecretMounts(settings.runtime_secrets_file,s.scope) if settings.runtime_secrets_file else None
    return RevisionCompatibility(s.db,s.scope,s.policy,resolver)


class CompatibilityReview(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_version:int=Field(ge=1)
    snapshot_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    evidence:str=Field(min_length=1,max_length=1000)


@router.get('/revisions/{revision_ref}/compatibility')
async def compatibility_detail(revision_ref:str,s=Depends(service)):
    return await compatibility_manager(s).show(revision_ref)


@router.post('/revisions/{revision_ref}/compatibility/observe')
async def compatibility_observe(revision_ref:str,s=Depends(service)):
    return await compatibility_manager(s).observe(revision_ref)


@router.post('/revisions/{revision_ref}/compatibility/accept')
async def compatibility_accept(revision_ref:str,body:CompatibilityReview,s=Depends(service)):
    return await compatibility_manager(s).accept(revision_ref,body.expected_version,body.snapshot_digest,'operator:console',body.evidence)


@router.get('/deployments')
async def deployments(after:str=Query('',max_length=160),limit:int=Query(50,ge=1,le=100),s=Depends(service)):
    return {'items':await deployment_manager(s).list(after=after,limit=limit)}


@router.get('/deployments/{deployment_id}')
async def deployment_detail(deployment_id:str,s=Depends(service)):
    return await deployment_manager(s).show(deployment_id)


@router.post('/deployments',status_code=202)
async def deployment_request(body:DeploymentRequest,s=Depends(service)):
    return await deployment_manager(s).request(body.id,body.revision_ref,body.idempotency_key,
        body.expected_version,'operator:console',body.probe_input,timeout=body.timeout,rollback=body.rollback)


@router.post('/deployments/{deployment_id}/reconcile')
async def deployment_reconcile(deployment_id:str,s=Depends(service)):
    return await deployment_manager(s).reconcile(deployment_id)

@router.get('/samples')
async def samples(s=Depends(service)):
    return {'items':await s.samples()}

@router.get('/workers')
async def workers(s=Depends(service)):
    if s.principal is not None:raise HTTPException(403,detail='AUTH_ACTION_DENIED')
    return await s.store.runtime_status()

@router.get('/runs')
async def listing(limit:int=Query(30,ge=1,le=100),cursor:str|None=Query(None,max_length=512),profile_id:str|None=Query(None,min_length=1,max_length=160),s=Depends(service)):
    return await s.list(limit=limit,cursor=cursor,profile_id=profile_id)

@router.get('/overview')
async def overview(period:str=Query('hour',pattern='^(hour|day)$'),buckets:int=Query(24,ge=1,le=168),
                   profile_id:str|None=Query(None,min_length=1,max_length=160),s=Depends(service)):
    return await s.overview(period=period,buckets=buckets,profile_id=profile_id)

@router.get('/scheduler-health')
async def scheduler_health(s=Depends(service)):
    if s.principal is not None:raise HTTPException(403,detail='AUTH_ACTION_DENIED')
    from app.runtime.monitor import snapshot
    return await snapshot(s.db,s.scope)

async def monitor_service(request:Request):
    from app.services.monitor_access import accepts
    auth=request.headers.get('authorization','')
    token=auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    if not settings.runtime_console_enabled:raise HTTPException(503,detail='RUNTIME_CONSOLE_DISABLED')
    # The independent credential is accepted only by this fixed metrics route.
    if await asyncio.to_thread(accepts,token,settings.access_token):
        value=getattr(request.app.state,'runtime_console',None)
        if value is None:raise HTTPException(503,detail='RUNTIME_CONFIGURATION_REQUIRED')
        return value
    principal=await operator(request)
    if principal is not None:raise HTTPException(403,detail='AUTH_ACTION_DENIED')
    return await service(request,principal)


@router.get('/scheduler-metrics')
async def scheduler_metrics(request:Request,s=Depends(monitor_service)):
    from app.runtime.monitor import snapshot, prometheus
    value=prometheus(await snapshot(s.db,s.scope))
    await monitor_service(request)
    return Response(value,media_type='text/plain; version=0.0.4; charset=utf-8')


class CapacityUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1,max_length=160)
    selector: dict
    max_concurrent: int = Field(ge=0,le=100000)
    expected_revision: int = Field(ge=0)
    enabled: bool = True
    budget: dict | None = None


@router.get('/capacity')
async def capacity_status(s=Depends(service)):
    if s.principal is not None:raise HTTPException(403,detail='AUTH_ACTION_DENIED')
    from app.runtime.capacity import Capacity
    return await Capacity(s.store).status()


@router.put('/capacity')
async def capacity_update(body:CapacityUpdate,s=Depends(service)):
    if s.principal is not None:raise HTTPException(403,detail='AUTH_ACTION_DENIED')
    from app.runtime.capacity import Capacity
    return await Capacity(s.store).put(body.id,body.selector,body.max_concurrent,
        body.expected_revision,enabled=body.enabled,source='operator_api',budget=body.budget)

@router.post('/runs',status_code=202)
async def submit(body:Submit,s=Depends(service)):
    return await s.submit(body.profile_id,body.sample_id,body.goal,body.idempotency_key,
        input_payload=body.input_payload,delegation_ref=body.delegation_ref,parent_run_id=body.parent_run_id,
        expected_profile_digest=body.expected_profile_digest)

@router.get('/runs/{run_id}')
async def detail(run_id:str,s=Depends(service)):
    return await s.detail(run_id)

@router.get('/runs/{run_id}/evidence')
async def evidence(run_id:str,section:str='attempts',limit:int=50,cursor:str|None=None,s=Depends(service)):
    from app.services.run_evidence import RunEvidence
    return await RunEvidence(s).view(run_id,section=section,limit=limit,cursor=cursor)

@router.post('/runs/{run_id}/cancel')
async def cancel(run_id:str,body:Cancel,s=Depends(service)):
    await s.detail(run_id)
    await s.authorize(run_id,'run.cancel')
    await s.store.cancel(run_id,body.expected_version,access_guard=lambda run:s.check_access(run,'run.cancel'))
    return await s.detail(run_id)

@router.post('/runs/{run_id}/revoke')
async def revoke(run_id:str,body:Cancel,s=Depends(service)):
    await s.detail(run_id)
    await s.authorize(run_id,'run.revoke')
    await s.store.revoke(run_id,body.expected_version,access_guard=lambda run:s.check_access(run,'run.revoke'))
    return await s.detail(run_id)

@router.get('/runs/{run_id}/artifacts/{artifact_id}/preview')
async def preview(run_id:str,artifact_id:str,s=Depends(service)):
    return await s.preview(run_id,artifact_id)

@router.get('/runs/{run_id}/diagnostic-export')
async def diagnostic_export(run_id:str,s=Depends(service)):
    from fastapi.responses import Response
    from uuid import uuid4
    value=await s.diagnostic_export(run_id)
    return Response(value,media_type='application/zip',headers={
        'Content-Disposition':f'attachment; filename="dios-diagnostic-{uuid4()}.zip"',
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@router.get('/runs/{run_id}/artifacts/{artifact_id}/download')
async def download(run_id:str,artifact_id:str,format:str=Query('raw',pattern='^(raw|markdown)$'),s=Depends(service)):
    headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
    if format=='markdown':
        value=await s.preview(run_id,artifact_id)
        if value['markdown'] is None:raise HTTPException(409,detail='MARKDOWN_NOT_AVAILABLE')
        headers['Content-Disposition']=f'attachment; filename="report-{artifact_id}.md"'
        return Response(value['markdown'],media_type='text/markdown; charset=utf-8',headers=headers)
    blob=await s.artifact(run_id,artifact_id)
    headers['Content-Disposition']=f'attachment; filename="artifact-{artifact_id}.bin"'
    headers['Content-Length']=str(blob['size'])
    return StreamingResponse(s.stream_artifact(run_id,blob),media_type='application/octet-stream',headers=headers)


async def tool_service(s=Depends(service)):
    if s.tools is None:raise HTTPException(503,detail='TOOL_GATE_NOT_CONFIGURED')
    from app.services.runtime_access import AuthorizedTools
    return AuthorizedTools(s)


class ToolRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    attempt_id: str=Field(min_length=1,max_length=64)
    session_id: str=Field(min_length=1,max_length=160)
    tool_id: str=Field(min_length=1,max_length=160)
    arguments: dict
    idempotency_key: str=Field(min_length=1,max_length=160)


class ToolApproval(Cancel):
    decision: str=Field(pattern='^(approved|denied)$')
    reason: str=Field(default='',max_length=2000)


@router.get('/runs/{run_id}/tools')
async def tool_list(run_id:str,s=Depends(tool_service)):
    return await s.list(run_id)


@router.post('/runs/{run_id}/tools',status_code=202)
async def tool_request(run_id:str,body:ToolRequest,s=Depends(tool_service)):
    return await s.request(run_id,body.attempt_id,body.session_id,body.tool_id,body.arguments,body.idempotency_key)


@router.get('/runs/{run_id}/tools/{invocation_id}')
async def tool_detail(run_id:str,invocation_id:str,s=Depends(tool_service)):
    return await s.get(run_id,invocation_id)


@router.post('/runs/{run_id}/tools/{invocation_id}/approval')
async def tool_approval(run_id:str,invocation_id:str,body:ToolApproval,request:Request,s=Depends(tool_service)):
    # Approval identity is resolved from a separate credential; clients cannot submit an actor.
    token=request.headers.get('x-dios-approver-token','')
    if not token or secrets.compare_digest(token.encode(),settings.access_token.encode()):
        raise HTTPException(403,detail='APPROVER_SEPARATE_CREDENTIAL_REQUIRED')
    return await s.decide(run_id,invocation_id,body.expected_version,body.decision,token,body.reason)


@router.post('/runs/{run_id}/tools/{invocation_id}/dispatch')
async def tool_dispatch(run_id:str,invocation_id:str,body:Cancel,s=Depends(tool_service)):
    return await s.dispatch(run_id,invocation_id,body.expected_version)


@router.post('/runs/{run_id}/tools/{invocation_id}/reconcile')
async def tool_reconcile(run_id:str,invocation_id:str,body:Cancel,s=Depends(tool_service)):
    return await s.reconcile_overdue(run_id,invocation_id,body.expected_version)


@router.post('/runs/{run_id}/tools/{invocation_id}/receipt-recover')
async def tool_receipt_recover(run_id:str,invocation_id:str,body:Cancel,s=Depends(tool_service)):
    return await s.recover_receipt(run_id,invocation_id,body.expected_version)


from .runtime_objects import register as register_objects
register_objects(router, service)

from .runtime_packages import register as register_packages
register_packages(router, service)

from .runtime_consumption import register as register_consumption
register_consumption(router, service)

from .runtime_knowledge import register as register_knowledge
register_knowledge(router, service)
