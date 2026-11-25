"""Trusted local administration. Only the explicit worker command launches workloads."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from sqlalchemy import select
from .database import Database, RuntimeErrorCode
from .store import RuntimeStore
from .schema import runs
from .spool import ReportSpool
from .history import HistoryQueries
from .lifecycle import Lifecycle
from .recovery import backup_sqlite, verify_backup, restore_sqlite

async def execute(args):
    if args.command == 'spool-status':
        return await asyncio.to_thread(ReportSpool.inspect, args.file)
    if args.command == 'backup-verify': return await asyncio.to_thread(verify_backup,args.bundle)
    if args.command == 'restore': return await restore_sqlite(args.bundle,args.destination)
    if args.command=='remote-worker':
        from .remote_runtime import serve_remote
        return await serve_remote(args)
    if not args.database_url:
        raise RuntimeErrorCode('RUNTIME_DATABASE_URL_REQUIRED')
    if args.command == 'backup':
        return await asyncio.to_thread(backup_sqlite,args.database_url,args.artifacts,args.destination,timeout=args.timeout)
    db = Database(args.database_url)
    try:
        if args.command == 'tool-gateway':
            from .gateway_admin import serve_gateway
            return await serve_gateway(args,db)
        if args.command == 'migrate': return await db.migrate()
        if args.command == 'revision':
            from .revisions import Revisions, MAX_MANIFEST_BYTES
            manager = Revisions(db, args.scope)
            if args.revision_command == 'publish-profile':
                from .execution_revisions import publish_profile
                return await publish_profile(db,args.scope,args.profile,args.key,args.actor)
            if args.revision_command == 'show':
                return await manager.show(args.revision_id, include_manifest=args.manifest)
            if args.revision_command == 'list':
                return await manager.list(args.definition_id, after=args.after, limit=args.limit)
            with Path(args.file).open('rb') as source:
                raw = source.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise RuntimeErrorCode('REVISION_MANIFEST_TOO_LARGE')
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise RuntimeErrorCode('REVISION_MANIFEST_INVALID') from None
            return await manager.create(args.key, value, actor=args.actor)
        if args.command == 'upgrade':
            lifecycle=Lifecycle(db,args.scope)
            if args.upgrade_command=='drain':return await lifecycle.begin_drain()
            if args.upgrade_command=='status':return await lifecycle.status()
            if args.upgrade_command=='wait':return await lifecycle.wait_drained(args.timeout)
            if args.upgrade_command=='resume':return await lifecycle.resume(args.revision)
        if args.command == 'history':
            query = HistoryQueries(db, args.scope)
            if args.history_command == 'list':
                return await query.list_imported(source=args.source, kind=args.kind, limit=args.limit, cursor=args.cursor)
            if args.history_command == 'show':
                return await query.imported(args.source, args.kind, args.source_id, include_record=args.record)
            if args.history_command == 'links':
                return await query.related(args.source, args.kind, args.source_id,
                    direction=args.direction, limit=args.limit, cursor=args.cursor)
            if args.history_command == 'run':
                return await query.run(args.run_id, limit=args.limit,
                    attempt_cursor=args.attempt_cursor, artifact_cursor=args.artifact_cursor)
        store = RuntimeStore(db, args.scope)
        if args.command == 'deployment':
            from .deployments import Deployments
            from .work_policy import FileWorkPolicy
            manager=Deployments(db,args.scope,FileWorkPolicy(args.policy_file) if args.policy_file else None)
            if args.deployment_command=='list':return {'items':await manager.list(after=args.after,limit=args.limit)}
            if args.deployment_command=='show':return await manager.show(args.id)
            if args.deployment_command=='reconcile':return await manager.reconcile(args.id)
            with Path(args.file).open('rb') as source:raw=source.read(16385)
            if len(raw)>16384:raise RuntimeErrorCode('METADATA_TOO_LARGE')
            value=json.loads(raw)
            if (not isinstance(value,dict) or set(value)-{'id','revision_ref','key','expected_version','actor','probe_input','timeout','rollback'}
                    or not {'id','key','expected_version','actor','probe_input'}<=set(value)):
                raise RuntimeErrorCode('INVALID_DEPLOYMENT_REQUEST')
            return await manager.request(value['id'],value.get('revision_ref'),value['key'],value['expected_version'],
                value['actor'],value['probe_input'],timeout=value.get('timeout',300),rollback=value.get('rollback',False))
        if args.command == 'capacity':
            from .capacity import Capacity
            manager = Capacity(store)
            if args.capacity_command == 'status':return await manager.status()
            with Path(args.file).open('rb') as source:raw=source.read(16385)
            if len(raw)>16384:raise RuntimeErrorCode('METADATA_TOO_LARGE')
            value=json.loads(raw)
            if (not isinstance(value,dict) or set(value)-{'id','selector','max_concurrent','enabled','budget'}
                    or not {'id','selector','max_concurrent'}<=set(value)):
                raise RuntimeErrorCode('INVALID_CAPACITY_LIMIT')
            return await manager.put(value['id'],value['selector'],value['max_concurrent'],
                args.revision,enabled=value.get('enabled',True),budget=value.get('budget'))
        if args.command=='delegation':
            from .delegations import Delegations
            manager=Delegations(store)
            if args.delegation_command=='show':return await manager.show(args.reference)
            if args.delegation_command=='revoke':return await manager.revoke(args.reference,args.version)
            with Path(args.file).open('rb') as source:raw=source.read(32769)
            if len(raw)>32768:raise RuntimeErrorCode('METADATA_TOO_LARGE')
            value=json.loads(raw)
            if not isinstance(value,dict) or set(value)-{'key','subject','actor','permissions','expires_at','policy_evaluator','parent_ref','issuer_actor'}:
                raise RuntimeErrorCode('INVALID_DELEGATION_REQUEST')
            if not {'key','subject','actor','permissions','expires_at'}<=set(value):raise RuntimeErrorCode('INVALID_DELEGATION_REQUEST')
            return await manager.issue(**value)
        if args.command == 'identity-events':
            from .logout_schema import events, check
            if not 1<=args.limit<=100:raise RuntimeErrorCode('INVALID_LIMIT')
            async with db.read(args.scope) as c:
                if not await check(c):raise RuntimeErrorCode('LOGOUT_MIGRATION_REQUIRED')
                rows=(await c.execute(select(events.c.id,events.c.issuer,events.c.client_id,events.c.issued_at,events.c.received_at,events.c.revoked_work_count)
                    .where(events.c.scope==args.scope).order_by(events.c.received_at.desc(),events.c.id).limit(args.limit))).mappings().all()
                return {'items':[dict(x) for x in rows]}
        if args.command == 'delegation-reconcile':
            from .delegation_reconciliation import DelegationReconciler
            return await DelegationReconciler(store,interval=args.interval,
                retry_base=args.retry_base,retry_max=args.retry_max).batch(limit=args.limit)
        if args.command == 'identity-reconcile':
            from .identity_reconciliation import IdentityReconciler
            from .work_policy import FileWorkPolicy
            if not args.policy_file:raise RuntimeErrorCode('WORK_POLICY_REQUIRED')
            return await IdentityReconciler(store,FileWorkPolicy(args.policy_file),
                interval=args.interval,retry_base=args.retry_base,retry_max=args.retry_max).batch(limit=args.limit)
        if args.command in {'enqueue', 'worker'}:
            from .worker import Worker, read_profile
            from .artifacts import LocalArtifacts
            from .docker_adapter import DockerRuntimeAdapter
            from .work_policy import FileWorkPolicy, security_profile
            from .work_authorizations import WorkAuthorizationVerifier
            from .secret_provider import FileSecretMounts
            deployment_id=getattr(args,'deployment_id',None)
            if deployment_id is not None:
                from .deployments import row as deployment_row
                async with db.read(args.scope) as c:active=await deployment_row(c,args.scope,deployment_id)
                if active['state'] not in {'ready','pending'} or active['active_revision_id'] is None:raise RuntimeErrorCode('DEPLOYMENT_NOT_READY')
                args.revision_ref=active['active_revision_id']
            if args.revision_ref:
                from .execution_revisions import load_profile
                _,profile_id,spec,mode,secret_requests=await load_profile(db,args.scope,args.revision_ref)
            else:
                profile_id,spec=read_profile(args.profile)
                mode,secret_requests=security_profile(args.profile)
            policy_path=args.policy_file
            policy=FileWorkPolicy(policy_path) if policy_path else None
            if mode=='e2ag' and policy is None:raise RuntimeErrorCode('WORK_POLICY_REQUIRED')
            if args.command == 'enqueue':
                payload = json.loads(Path(args.file).read_text(encoding='utf-8')) if args.file else {}
                descriptor=await asyncio.to_thread(policy.admission,profile_id,spec.fingerprint(),secret_requests) if mode=='e2ag' else None
                return await store.submit(args.key,{'runtime_profile':profile_id,
                    'profile_digest':spec.fingerprint(),'secret_refs':secret_requests,'payload':payload},authorization=descriptor,revision_ref=args.revision_ref,deployment_id=deployment_id)
            artifacts=LocalArtifacts(Path(args.artifacts))
            secrets=FileSecretMounts(args.secrets_file,args.scope) if args.secrets_file else None
            from .a2a_profile import A2AProfile
            if isinstance(spec,A2AProfile):
                from .a2a_worker import A2AWorker
                if args.tools_file or args.approvers_file or args.tool_gateway_file:
                    raise RuntimeErrorCode('A2A_REMOTE_TOOL_CONTROL_UNAVAILABLE')
                runner=A2AWorker(store,profile_id,spec,artifacts,policy,secret_resolver=secrets,
                    secret_requests=secret_requests,timeout=args.timeout,poll_interval=args.poll_interval,revision_ref=args.revision_ref)
                return await runner.serve(once=args.once)
            tool_gate=None;remote_tools=None
            from .worker_tool_channel import POLICY_ENV
            if args.tool_gateway_file:
                from .remote_tool_channel import RemoteToolProvider
                if args.tools_file or args.approvers_file:raise RuntimeErrorCode('TOOL_PROVIDER_CONFLICT')
                remote_tools=RemoteToolProvider(args.tool_gateway_file,store,profile_id,spec)
            if POLICY_ENV in spec.environment and remote_tools is None:
                if not args.tools_file:raise RuntimeErrorCode('TOOL_GATE_NOT_CONFIGURED')
                from .tool_invocations import ToolInvocations
                from .tool_catalog import FileToolCatalog, FileApprovers
                from .mcp_tool_transport import McpHttpTransport
                tool_gate=ToolInvocations(store,policy,FileToolCatalog(args.tools_file),artifacts,McpHttpTransport(),
                    FileApprovers(args.approvers_file) if args.approvers_file else None,secrets)
            runner = Worker(store,DockerRuntimeAdapter(args.scope),profile_id,spec,artifacts,
                timeout=args.timeout,poll_interval=args.poll_interval,
                authorization_verifier=WorkAuthorizationVerifier(store,policy) if mode=='e2ag' else None,
                secret_requests=secret_requests,
                secret_resolver=secrets,tool_gate=tool_gate,remote_tools=remote_tools,revision_ref=args.revision_ref)
            return await runner.serve(once=args.once)
        if args.command == 'credential':
            from .worker_credentials import WorkerCredentials
            service=WorkerCredentials(store,None,None,'unused')
            if args.credential_command=='status':return await service.status(args.run_id)
            return await service.revoke(args.credential_id,args.revision)
        if args.command == 'spool-flush':
            from .reporting import relay_reports
            # Explicit file must exist; inspection checks its schema without creating it.
            await asyncio.to_thread(ReportSpool.inspect,args.file)
            return await relay_reports(store,ReportSpool(args.file))
        if args.command == 'reconcile':
            from .reconciliation import reconcile_run
            from .artifacts import LocalArtifacts
            from .docker_adapter import DockerRuntimeAdapter
            from .work_policy import FileWorkPolicy
            from .work_authorizations import WorkAuthorizationVerifier
            verifier=WorkAuthorizationVerifier(store,FileWorkPolicy(args.policy_file)) if args.policy_file else None
            return await reconcile_run(store,DockerRuntimeAdapter(args.scope),
                LocalArtifacts(Path(args.artifacts)),args.run_id,verifier=verifier)
        if args.command == 'circuits':
            from .fault_schema import circuits
            async with db.read(args.scope) as c:
                return [dict(x) for x in (await c.execute(select(circuits).where(circuits.c.scope==args.scope))).mappings()]
        if args.command == 'workers': return await store.runtime_status(fresh_seconds=args.fresh_seconds)
        if args.command == 'scheduler-health':
            from .monitor import snapshot
            return await snapshot(db,args.scope)
        if args.command == 'overview':
            from .overview import view
            from app.services.task_console import console_filter
            result=await view(db,args.scope,console_filter(args.scope,args.profile_id),period=args.period,buckets=args.buckets)
            result['profile_id']=args.profile_id
            return result
        if args.command == 'cancel': return await store.cancel(args.run_id,args.revision)
        if args.command == 'revoke':
            await store.revoke(args.run_id,args.revision)
            return await store.get(args.run_id)
        if args.command == 'expire': return {'expired':await store.expire()}
        if args.command in {'runtime-inspect','runtime-cleanup'}:
            from dataclasses import asdict
            from .docker_adapter import DockerRuntimeAdapter
            from .store import TERMINAL
            run = await store.get(args.run_id)
            attempt = next((a for a in run['attempts'] if a['id']==run['attempt_id']), {})
            launch = attempt.get('remote',{}).get('launch',{})
            if launch.get('adapter') != DockerRuntimeAdapter.name:
                raise RuntimeErrorCode('RUNTIME_BINDING_NOT_FOUND')
            adapter = DockerRuntimeAdapter(args.scope)
            if args.command == 'runtime-inspect': return asdict(await adapter.observe(launch['handle']))
            if run['state'] not in TERMINAL: raise RuntimeErrorCode('RUN_NOT_TERMINAL')
            await adapter.remove(launch['handle'])
            return {'run_id':args.run_id,'removed':True}
        if args.command == 'submit':
            return await store.submit(args.key, json.loads(Path(args.file).read_text(encoding='utf-8')))
        if args.command == 'show': return await store.get(args.run_id,diagnostics=True)
        if args.command == 'events': return await store.pending_events(args.consumer, args.limit)
        if args.command == 'drain':
            await store.drain(args.worker_id)
            return {'worker_id': args.worker_id, 'draining': True}
        if args.command == 'list':
            async with db.engine.connect() as c:
                await db.check_schema(c)
                return [dict(x) for x in (await c.execute(select(runs.c.id,runs.c.state,runs.c.version,runs.c.updated_at)
                    .where(runs.c.scope==args.scope).order_by(runs.c.created_at.desc()).limit(args.limit))).mappings()]
    finally:
        await db.close()

def main(argv=None):
    p = argparse.ArgumentParser(description='DiOS durable runtime local administration')
    p.add_argument('--database-url',default=os.environ.get('DIOS_RUNTIME_DATABASE_URL'))
    p.add_argument('--scope',default='local')
    commands = p.add_subparsers(dest='command',required=True)
    commands.add_parser('migrate',help='explicit additive migration; backup existing database first')
    revision = commands.add_parser('revision', help='immutable configuration snapshots; does not launch or authorize tasks')
    revision_commands = revision.add_subparsers(dest='revision_command', required=True)
    revision_create = revision_commands.add_parser('create')
    revision_create.add_argument('--file', required=True)
    revision_create.add_argument('--key', required=True)
    revision_create.add_argument('--actor', required=True, help='audit label of the trusted local publisher')
    revision_publish = revision_commands.add_parser('publish-profile', help='freeze a local execution profile and resolved image identity')
    revision_publish.add_argument('--profile', required=True)
    revision_publish.add_argument('--key', required=True)
    revision_publish.add_argument('--actor', required=True)
    revision_show = revision_commands.add_parser('show')
    revision_show.add_argument('revision_id')
    revision_show.add_argument('--manifest', action='store_true')
    revision_list = revision_commands.add_parser('list')
    revision_list.add_argument('definition_id')
    revision_list.add_argument('--after', type=int, default=0)
    revision_list.add_argument('--limit', type=int, default=50)
    backup=commands.add_parser('backup',help='snapshot SQLite and referenced local artifacts to a new directory')
    backup.add_argument('--artifacts',required=True);backup.add_argument('--destination',required=True)
    backup.add_argument('--timeout',type=float,default=60)
    verify=commands.add_parser('backup-verify');verify.add_argument('bundle')
    restore=commands.add_parser('restore',help='restore into a new directory with scheduling held')
    restore.add_argument('bundle');restore.add_argument('destination')
    upgrade=commands.add_parser('upgrade',help='persistent scope drain; active work finishes naturally')
    upgrades=upgrade.add_subparsers(dest='upgrade_command',required=True)
    upgrades.add_parser('drain');upgrades.add_parser('status')
    waiting=upgrades.add_parser('wait');waiting.add_argument('--timeout',type=float,default=30)
    resume=upgrades.add_parser('resume');resume.add_argument('--revision',type=int,required=True)
    spool = commands.add_parser('spool-status', help='inspect retained Worker reports without payloads or network calls')
    spool.add_argument('--file', required=True)
    relay = commands.add_parser('spool-flush',help='deliver a bounded batch using original attempt and authorization epochs')
    relay.add_argument('--file',required=True)
    reconcile = commands.add_parser('reconcile',help='validate original exited workload; never launch or requeue')
    reconcile.add_argument('run_id');reconcile.add_argument('--artifacts',required=True)
    reconcile.add_argument('--policy-file',default=os.environ.get('DIOS_RUNTIME_POLICY_FILE'))
    submit = commands.add_parser('submit',help='queue metadata only; does not launch an Agent')
    submit.add_argument('--key',required=True);submit.add_argument('--file',required=True)
    show = commands.add_parser('show');show.add_argument('run_id')
    delegation=commands.add_parser('delegation',help='trusted local administration of immutable delegation records')
    delegation_commands=delegation.add_subparsers(dest='delegation_command',required=True)
    issue=delegation_commands.add_parser('issue');issue.add_argument('--file',required=True)
    inspect_delegation=delegation_commands.add_parser('show');inspect_delegation.add_argument('reference')
    revoke_delegation=delegation_commands.add_parser('revoke');revoke_delegation.add_argument('reference');revoke_delegation.add_argument('--version',type=int,required=True)
    enqueue=commands.add_parser('enqueue',help='queue a pinned trusted profile and optional JSON input')
    enqueue_source=enqueue.add_mutually_exclusive_group(required=True)
    enqueue_source.add_argument('--profile');enqueue_source.add_argument('--revision-ref');enqueue_source.add_argument('--deployment-id')
    enqueue.add_argument('--key',required=True);enqueue.add_argument('--file')
    worker=commands.add_parser('worker',help='run an independent one-slot Worker using an explicit trusted profile')
    worker_source=worker.add_mutually_exclusive_group(required=True)
    worker_source.add_argument('--profile');worker_source.add_argument('--revision-ref')
    worker.add_argument('--artifacts',required=True)
    worker.add_argument('--once',action='store_true');worker.add_argument('--timeout',type=float,default=300)
    worker.add_argument('--poll-interval',type=float,default=.25)
    for command in (enqueue,worker):command.add_argument('--policy-file',default=os.environ.get('DIOS_RUNTIME_POLICY_FILE'))
    worker.add_argument('--secrets-file',default=os.environ.get('DIOS_RUNTIME_SECRETS_FILE'))
    worker.add_argument('--tools-file',default=os.environ.get('DIOS_RUNTIME_TOOLS_FILE'))
    worker.add_argument('--approvers-file',default=os.environ.get('DIOS_RUNTIME_APPROVERS_FILE'))
    worker.add_argument('--tool-gateway-file',help='host-only mTLS tool Provider configuration')
    remote=commands.add_parser('remote-worker',help='mTLS Worker; no database URL or shared artifacts required')
    remote.add_argument('--control-file',required=True)
    remote_source=remote.add_mutually_exclusive_group(required=True)
    remote_source.add_argument('--profile');remote_source.add_argument('--revision-file')
    remote.add_argument('--artifacts',required=True);remote.add_argument('--secrets-file')
    remote.add_argument('--once',action='store_true');remote.add_argument('--timeout',type=float,default=300)
    remote.add_argument('--poll-interval',type=float,default=.25)
    deployment=commands.add_parser('deployment',help='governed candidate health, natural drain and activation')
    deployment.add_argument('--policy-file',default=os.environ.get('DIOS_RUNTIME_POLICY_FILE'))
    deployment_commands=deployment.add_subparsers(dest='deployment_command',required=True)
    deployment_list=deployment_commands.add_parser('list');deployment_list.add_argument('--after',default='');deployment_list.add_argument('--limit',type=int,default=50)
    deployment_show=deployment_commands.add_parser('show');deployment_show.add_argument('id')
    deployment_reconcile=deployment_commands.add_parser('reconcile');deployment_reconcile.add_argument('id')
    deployment_request=deployment_commands.add_parser('request');deployment_request.add_argument('--file',required=True)
    gateway=commands.add_parser('tool-gateway',help='serve the explicit mTLS tool Provider; no automatic migration')
    for name in ('config','artifacts','policy-file','tools-file','identities-file','profiles-file'):
        gateway.add_argument('--'+name,required=True)
    gateway.add_argument('--approvers-file');gateway.add_argument('--secrets-file')
    credential=commands.add_parser('credential',help='inspect or revoke host credentials without exposing secrets')
    credential_commands=credential.add_subparsers(dest='credential_command',required=True)
    credential_status=credential_commands.add_parser('status');credential_status.add_argument('run_id')
    credential_revoke=credential_commands.add_parser('revoke');credential_revoke.add_argument('credential_id')
    credential_revoke.add_argument('--revision',type=int,required=True)
    commands.add_parser('circuits',help='inspect persisted endpoint/account cooldown and single-probe ownership')
    capacity=commands.add_parser('capacity',help='trusted local shared execution concurrency administration')
    capacity_commands=capacity.add_subparsers(dest='capacity_command',required=True)
    capacity_commands.add_parser('status',help='reserved Attempts and unresolved work; not observed running processes')
    capacity_set=capacity_commands.add_parser('set',help='create at revision 0; replace using the current revision')
    capacity_set.add_argument('--file',required=True)
    capacity_set.add_argument('--revision',type=int,required=True)
    identity=commands.add_parser('identity-reconcile',help='bounded source check; retains Work/approval on outage, revokes confirmed invalid identities')
    delegation_check=commands.add_parser('delegation-reconcile',help='bounded issuer checks for delegations, including those without tasks')
    delegation_check.add_argument('--limit',type=int,default=20)
    delegation_check.add_argument('--interval',type=float,default=60)
    delegation_check.add_argument('--retry-base',type=float,default=15)
    delegation_check.add_argument('--retry-max',type=float,default=300)
    events=commands.add_parser('identity-events',help='read verified logout receipts in the selected scope')
    events.add_argument('--limit',type=int,default=20)
    identity.add_argument('--policy-file',default=os.environ.get('DIOS_RUNTIME_POLICY_FILE'))
    identity.add_argument('--limit',type=int,default=20)
    identity.add_argument('--interval',type=float,default=60)
    identity.add_argument('--retry-base',type=float,default=15)
    identity.add_argument('--retry-max',type=float,default=300)
    status=commands.add_parser('workers' ,help='persisted process/session evidence, freshness and actual concurrency')
    status.add_argument('--fresh-seconds',type=float,default=15)
    commands.add_parser('scheduler-health',help='persistent scheduler liveness and progress observations')
    overview=commands.add_parser('overview',help='durable console-task throughput and active-work observations in the trusted local scope')
    overview.add_argument('--period',choices=['hour','day'],default='hour')
    overview.add_argument('--buckets',type=int,default=24)
    overview.add_argument('--profile-id')
    cancel=commands.add_parser('cancel');cancel.add_argument('run_id');cancel.add_argument('--revision',required=True,type=int)
    revoke=commands.add_parser('revoke');revoke.add_argument('run_id');revoke.add_argument('--revision',required=True,type=int)
    commands.add_parser('expire',help='fence expired leases for reconciliation; does not requeue')
    for name in ('runtime-inspect','runtime-cleanup'):
        command=commands.add_parser(name);command.add_argument('run_id')
    listing = commands.add_parser('list');listing.add_argument('--limit',type=int,default=100)
    events = commands.add_parser('events');events.add_argument('--consumer',required=True);events.add_argument('--limit',type=int,default=100)
    drain = commands.add_parser('drain');drain.add_argument('worker_id')
    history = commands.add_parser('history', help='query native execution and imported identity relationships')
    history_commands = history.add_subparsers(dest='history_command', required=True)
    history_list = history_commands.add_parser('list', help='page imported identities without original records')
    history_list.add_argument('--source');history_list.add_argument('--kind')
    history_show = history_commands.add_parser('show', help='inspect an imported identity; raw record is opt-in')
    history_show.add_argument('--record', action='store_true')
    history_links = history_commands.add_parser('links', help='page explicit source-adapter relationships')
    history_links.add_argument('--direction', choices=['outgoing','incoming'], default='outgoing')
    for command in (history_show, history_links):
        command.add_argument('source');command.add_argument('kind');command.add_argument('source_id')
    for command in (history_list, history_links):
        command.add_argument('--limit', type=int, default=50);command.add_argument('--cursor')
    history_run = history_commands.add_parser('run', help='read a native Run with paged attempts and artifact references')
    history_run.add_argument('run_id');history_run.add_argument('--limit', type=int, default=50)
    history_run.add_argument('--attempt-cursor');history_run.add_argument('--artifact-cursor')
    args = p.parse_args(argv)
    if hasattr(args,'limit') and not 1 <= args.limit <= 1000:p.error('limit must be 1..1000')
    try:
        value = asyncio.run(execute(args))
        print(json.dumps(value,ensure_ascii=False))
        return 0
    except RuntimeErrorCode as e:
        print(json.dumps({'error':e.code}))
        return 2
    except Exception as e:
        # Database exceptions can include credential URLs or input. Do not echo them.
        print(json.dumps({'error':'RUNTIME_COMMAND_FAILED','type':type(e).__name__}))
        return 1

if __name__ == '__main__': raise SystemExit(main())
