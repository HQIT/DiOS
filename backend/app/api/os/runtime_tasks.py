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
                response=await original(request)
                response.headers["Cache-Control"]="no-store"
                response.headers["X-Content-Type-Options"]="nosniff"
                return response
            except RuntimeErrorCode as exc:
                status=404 if exc.code in {'NOT_FOUND','SAMPLE_NOT_FOUND','PROFILE_NOT_AVAILABLE'} else 409
                if exc.code.startswith('INVALID_'):status=422
                if exc.code.startswith('AUTH_'):status=403
                if exc.code=='AUTH_UNAUTHENTICATED':status=401
                if exc.code=='AUTH_PROVIDER_UNAVAILABLE':status=503
                if exc.code in {'PREVIEW_TOO_LARGE','PREVIEW_NOT_TEXT'}:status=413
                raise HTTPException(status,detail=exc.code) from None
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
    db=None;app.state.runtime_console=None
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

    try:yield
    finally:
        if db:await db.close()
        app.state.runtime_console=None


async def service(request:Request,_=Depends(operator)):
    value=getattr(request.app.state,'runtime_console',None)
    if value is None:raise HTTPException(503,detail='RUNTIME_CONFIGURATION_REQUIRED')
    return value.for_principal(_)


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

class Submit(BaseModel):
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

class Cancel(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: int=Field(ge=0)

@router.get('/identity')
async def identity(s=Depends(service)):
    if s.principal is None:return {'mode':'operator','principal':None}
    row=await asyncio.to_thread(s.access_row)
    return {'mode':'principal','principal':s.principal,'profiles':{k:v['actions'] for k,v in row['profiles'].items()}}

@router.get('/profiles')
async def profiles(s=Depends(service)):
    return {'items':await asyncio.to_thread(s.catalog)}

@router.get('/samples')
async def samples(s=Depends(service)):
    return {'items':await asyncio.to_thread(s.samples)}

@router.get('/workers')
async def workers(s=Depends(service)):
    if s.principal is not None:raise HTTPException(403,detail='AUTH_ACTION_DENIED')
    return await s.store.runtime_status()

@router.get('/runs')
async def listing(limit:int=Query(30,ge=1,le=100),cursor:str|None=Query(None,max_length=512),s=Depends(service)):
    return await s.list(limit=limit,cursor=cursor)

@router.post('/runs',status_code=202)
async def submit(body:Submit,s=Depends(service)):
    return await s.submit(body.profile_id,body.sample_id,body.goal,body.idempotency_key,input_payload=body.input_payload)

@router.get('/runs/{run_id}')
async def detail(run_id:str,s=Depends(service)):
    return await s.detail(run_id)

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
