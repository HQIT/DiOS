"""Explicit gateway command. Startup does not migrate or start Workers."""
import asyncio
import signal
from pathlib import Path
from .database import RuntimeErrorCode
from .store import RuntimeStore
from .artifacts import LocalArtifacts
from .work_policy import FileWorkPolicy
from .tool_catalog import FileToolCatalog, FileApprovers
from .secret_provider import FileSecretMounts
from .tool_invocations import ToolInvocations
from .mcp_tool_transport import McpHttpTransport
from .worker_identity import WorkerIdentities, document, tls_context
from .remote_tool_gateway import ToolGateway


async def serve_gateway(args,db):
    config=document(args.config)
    if config.get('enabled') is not True:raise RuntimeErrorCode('WORKER_GATEWAY_DISABLED')
    if type(config.get('port')) is not int or not 1<=config['port']<=65535:raise RuntimeErrorCode('WORKER_GATEWAY_CONFIG_INVALID')
    context=tls_context(config['ca_file'],config['certificate_file'],config['key_file'],server=True)
    identities=WorkerIdentities(args.identities_file,config['audience'])
    gate=ToolInvocations(RuntimeStore(db,args.scope),FileWorkPolicy(args.policy_file),FileToolCatalog(args.tools_file),
        LocalArtifacts(Path(args.artifacts)),McpHttpTransport(),
        FileApprovers(args.approvers_file) if args.approvers_file else None,
        FileSecretMounts(args.secrets_file,args.scope) if args.secrets_file else None)
    credentials=None
    if config.get('issuer_key_file'):
        from .worker_credentials import WorkerCredentials
        credentials=WorkerCredentials(gate.store,identities,gate.policy,config['issuer_key_file'],ttl=config.get('credential_ttl_seconds',900))
        await asyncio.to_thread(credentials.key)
    gateway=ToolGateway(gate,identities,args.profiles_file,credentials)
    stop=asyncio.Event();loop=asyncio.get_running_loop();installed=[]
    try:
        for sig in (signal.SIGINT,signal.SIGTERM):
            try:loop.add_signal_handler(sig,stop.set);installed.append(sig)
            except NotImplementedError:pass
        await gateway.serve(config.get('host','127.0.0.1'),config['port'],context,stop)
    finally:
        for sig in installed:loop.remove_signal_handler(sig)
    return {'state':'stopped','active_tool_messages':len(gateway.active)}
