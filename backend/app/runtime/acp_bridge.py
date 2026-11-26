"""ACP v1 stdio adapter -> the Worker JSONL bridge, for inline sample reviews.

The registered command/configuration is operator-owned. Prompt input cannot select
commands, credentials, directories or MCP endpoints. Native tool governance remains
an explicit #34 qualification gate; client operations use the Worker tool gate.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import sys
from uuid import uuid4
from .database import RuntimeErrorCode
from .store import encoded


def emit(value):
    sys.stdout.buffer.write(encoded(value)+b'\n');sys.stdout.buffer.flush()


def diagnostic(code):
    # Provider stderr/errors may include credentials; only normalized codes leave this bridge.
    sys.stderr.write(json.dumps({'code':code,'run_id':os.environ.get('DIOS_RUN_ID'),
        'attempt_id':os.environ.get('DIOS_ATTEMPT_ID')})+'\n');sys.stderr.flush()


class AcpClient:
    def __init__(self, process, *, emit_event=emit, limit=32000, tool_client=None, client_read_tool=None, permission_decoder=None, owns_process_group=False):
        self.process=process;self.emit=emit_event;self.limit=limit
        self.pending={};self.counter=0;self.session_id=None;self.output=''
        self.permission_decoder=permission_decoder
        self.tool_client=tool_client;self.client_read_tool=client_read_tool;self.tool_activity={}
        self.native_tool_seen=False;self.denials=0;self.reader=None;self.stderr=None;self.failure=None
        self.monitor=None;self.owns_process_group=owns_process_group

    def fail_pending(self, failure):
        self.failure=self.failure or failure
        for future in self.pending.values():
            if not future.done():future.set_exception(self.failure)

    async def wait_exited(self):
        # Process.wait() may still wait for inherited pipes after the direct child exits.
        while self.process.returncode is None:
            await asyncio.sleep(.02)

    async def watch_process(self):
        await self.wait_exited()
        # Give already-written final responses a bounded chance to drain.
        if self.reader:
            try:await asyncio.wait_for(asyncio.shield(self.reader),.5)
            except TimeoutError:pass
        self.fail_pending(RuntimeErrorCode('ACP_PROCESS_EXITED'))

    async def send(self, value):
        self.process.stdin.write(encoded(value)+b'\n')
        await self.process.stdin.drain()

    async def read(self):
        try:
            while line:=await self.process.stdout.readline():
                if len(line)>256*1024:raise RuntimeErrorCode('ACP_FRAME_TOO_LARGE')
                try:message=json.loads(line.decode('utf-8'))
                except (ValueError,UnicodeError):raise RuntimeErrorCode('ACP_FRAME_INVALID') from None
                if not isinstance(message,dict) or message.get('jsonrpc')!='2.0':raise RuntimeErrorCode('ACP_FRAME_INVALID')
                if 'method' in message:
                    await self.incoming(message)
                elif message.get('id') in self.pending:
                    future=self.pending[message['id']]
                    if not future.done():future.set_result(message)
            raise RuntimeErrorCode('ACP_PROCESS_EXITED')
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.fail_pending(exc if isinstance(exc,RuntimeErrorCode) else RuntimeErrorCode('ACP_TRANSPORT_FAILED'))

    async def discard_stderr(self):
        # Drain concurrently so a verbose child cannot deadlock RPC. Never forward raw credentials.
        while await self.process.stderr.read(8192):pass

    async def incoming(self, message):
        method=message['method'];params=message.get('params',{})
        if not isinstance(params,dict):raise RuntimeErrorCode('ACP_FRAME_INVALID')
        if method=='session/update':
            if self.session_id is None or params.get('sessionId')!=self.session_id:
                raise RuntimeErrorCode('SESSION_CHANGED')
            update=params.get('update',{})
            if not isinstance(update,dict):raise RuntimeErrorCode('ACP_FRAME_INVALID')
            if update.get('sessionUpdate') in {'tool_call','tool_call_update'}:
                self.native_tool_seen=True
                if self.tool_client:
                    call_id=update.get('toolCallId')
                    if not isinstance(call_id,str) or not call_id or len(call_id)>160:raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_UNVERIFIED')
                    if call_id not in self.tool_activity and len(self.tool_activity)>=128:raise RuntimeErrorCode('TOOL_CALL_LIMIT_EXCEEDED')
                    record=self.tool_activity.setdefault(call_id,{})
                    for field in ('name','title','rawInput'):
                        if field in update and update[field] is not None:
                            if field in record and record[field]!=update[field] and (record.get('status') in {'completed','failed'} or update.get('sessionUpdate')=='tool_call'):
                                raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_CHANGED')
                            record[field]=update[field]
                    if update.get('status') is not None:
                        if record.get('status') in {'completed','failed'} and record['status']!=update['status']:
                            raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_CHANGED')
                        record['status']=update['status']
                else:diagnostic('ACP_TOOL_ACTIVITY_UNVERIFIED')
            if update.get('sessionUpdate')=='agent_message_chunk':
                content=update.get('content',{})
                if content.get('type')=='text':
                    text=content.get('text')
                    if not isinstance(text,str):raise RuntimeErrorCode('ACP_FRAME_INVALID')
                    if len((self.output+text).encode())>self.limit:raise RuntimeErrorCode('OUTPUT_LIMIT_EXCEEDED')
                    self.output+=text
                    for start in range(0,len(text),2000):self.emit({'type':'output','text':text[start:start+2000]})
        if 'id' in message:
            if method=='fs/read_text_file' and self.tool_client and self.client_read_tool:
                try:
                    if params.get('sessionId')!=self.session_id:raise RuntimeErrorCode('SESSION_CHANGED')
                    if set(params)-{'sessionId','path','line','limit'}:raise RuntimeErrorCode('ACP_FILE_REQUEST_INVALID')
                    path=params.get('path')
                    if not isinstance(path,str) or not path.startswith('/') or len(path)>4096:raise RuntimeErrorCode('ACP_FILE_REQUEST_INVALID')
                    args={'path':path}
                    for field in ('line','limit'):
                        if field in params:
                            if type(params[field]) is not int or not 1<=params[field]<=100000:raise RuntimeErrorCode('ACP_FILE_REQUEST_INVALID')
                            args[field]=params[field]
                    value=await self.tool_client.call(self.client_read_tool,args,'acp-read:'+str(message['id']))
                    content=value.get('content',[])
                    if value.get('isError') or not content or any(not isinstance(c,dict) or c.get('type')!='text' or not isinstance(c.get('text'),str) for c in content):
                        raise RuntimeErrorCode('ACP_FILE_RESULT_INVALID')
                    text=''.join(c['text'] for c in content)
                    if len(text.encode())>65536:raise RuntimeErrorCode('ACP_FILE_RESULT_TOO_LARGE')
                    await self.send({'jsonrpc':'2.0','id':message['id'],'result':{'content':text}})
                except Exception as exc:
                    code=exc.code if isinstance(exc,RuntimeErrorCode) else 'TOOL_CHANNEL_UNAVAILABLE'
                    await self.send({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32000,'message':code}})
                return
            if method=='session/request_permission':
                if params.get('sessionId')!=self.session_id:raise RuntimeErrorCode('SESSION_CHANGED')
                options=params.get('options',[])
                if self.tool_client and self.permission_decoder:
                    try:
                        permission_call=params.get('toolCall',{})
                        if not isinstance(permission_call,dict):raise RuntimeErrorCode('ACP_FRAME_INVALID')
                        prior=self.tool_activity.get(permission_call.get('toolCallId'),{})
                        mapped=self.permission_decoder({**prior,**{k:v for k,v in permission_call.items() if v is not None}})
                        permit=await self.tool_client.request('permit_proxy',mapped)
                        allow=next((x for x in options if isinstance(x,dict) and x.get('kind')=='allow_once'),None)
                        if permit.get('forward_to_gate') is True and allow and isinstance(allow.get('optionId'),str):
                            await self.send({'jsonrpc':'2.0','id':message['id'],'result':{'outcome':{'outcome':'selected','optionId':allow['optionId']}}})
                            return
                    except (RuntimeErrorCode,KeyError,TypeError):pass
                reject=next((x for x in options if isinstance(x,dict) and x.get('kind')=='reject_once'),None)
                outcome={'outcome':'selected','optionId':reject['optionId']} if reject else {'outcome':'cancelled'}
                await self.send({'jsonrpc':'2.0','id':message['id'],'result':{'outcome':outcome}})
                self.denials+=1;diagnostic('TOOL_PERMISSION_DENIED')
                if self.tool_client:await self.tool_client.request('deny_native',{'method':'session/request_permission'})
            else:
                await self.send({'jsonrpc':'2.0','id':message['id'],
                    'error':{'code':-32601,'message':'Client operation is not enabled'}})
                if self.tool_client:
                    normalized='terminal' if method.startswith('terminal/') else 'fs/write_text_file' if method=='fs/write_text_file' else 'unsupported'
                    await self.tool_client.request('deny_native',{'method':normalized})

    async def call(self, method, params, timeout=30):
        if self.failure:raise self.failure
        self.counter+=1;key=self.counter
        future=asyncio.get_running_loop().create_future();self.pending[key]=future
        try:
            await self.send({'jsonrpc':'2.0','id':key,'method':method,'params':params})
            try:response=await asyncio.wait_for(future,timeout)
            except TimeoutError:raise RuntimeErrorCode('ACP_QUERY_TIMEOUT') from None
            if 'error' in response:
                diagnostic('ACP_REMOTE_ERROR')
                raise RuntimeErrorCode('ACP_AUTH_FAILED' if method=='authenticate' else 'ACP_REQUEST_FAILED')
            result=response.get('result')
            if not isinstance(result,dict):raise RuntimeErrorCode('ACP_FRAME_INVALID')
            return result
        finally:self.pending.pop(key,None)

    async def cancel(self):
        if self.session_id:
            await self.send({'jsonrpc':'2.0','method':'session/cancel','params':{'sessionId':self.session_id}})

    async def close(self):
        def stop(sig):
            try:
                if self.owns_process_group:os.killpg(self.process.pid,sig)
                elif self.process.returncode is None:self.process.send_signal(sig)
            except ProcessLookupError:pass
        try:
            if self.process.returncode is None:
                try:await asyncio.wait_for(self.cancel(),.5)
                except (OSError,ConnectionError,TimeoutError):pass
            self.process.stdin.close()
            try:await asyncio.wait_for(self.wait_exited(),3)
            except TimeoutError:
                stop(signal.SIGTERM)
                try:await asyncio.wait_for(self.wait_exited(),2)
                except TimeoutError:
                    stop(signal.SIGKILL)
                    await asyncio.wait_for(self.wait_exited(),2)
        finally:
            # Reap the private group even when its leader died with a descendant holding pipes.
            stop(signal.SIGKILL)
            for task in (self.reader,self.stderr,self.monitor):
                if task:task.cancel()
            await asyncio.gather(*(t for t in (self.reader,self.stderr,self.monitor) if t),return_exceptions=True)
            # asyncio has no public Process.close(); close owned pipe transports before loop exit.
            self.process._transport.close()
            await asyncio.wait_for(self.process.wait(),2)


def configuration(path):
    source=Path(path)
    if source.stat().st_size>16384:raise RuntimeErrorCode('ACP_CONFIG_INVALID')
    config=json.loads(source.read_text(encoding='utf-8'))
    command=config.get('command')
    if not isinstance(command,list) or not command or not all(isinstance(x,str) and x for x in command):
        raise RuntimeErrorCode('ACP_CONFIG_INVALID')
    if not Path(command[0]).is_absolute():raise RuntimeErrorCode('ACP_EXECUTABLE_MUST_BE_ABSOLUTE')
    cwd=Path(config.get('cwd','/tmp')).resolve()
    if not cwd.is_dir():raise RuntimeErrorCode('ACP_WORKSPACE_MISSING')
    env=dict(os.environ)
    # A launcher adapter for preprovisioned files; enterprise Secret lifecycle is #33.
    for name,path in config.get('credential_env_files',{}).items():
        if not re.fullmatch('[A-Z][A-Z0-9_]{0,79}',name):raise RuntimeErrorCode('ACP_CONFIG_INVALID')
        secret=Path(path)
        if secret.stat().st_size>16384:raise RuntimeErrorCode('ACP_CREDENTIAL_INVALID')
        env[name]=secret.read_text(encoding='utf-8').strip()
        if not env[name]:raise RuntimeErrorCode('ACP_CREDENTIAL_INVALID')
    return config,command,cwd,env


async def execute(path, prompt, decode_result, *, required_tools=(), activity_decoder=None, output_decoder=None, permission_decoder=None):
    """Run one ACP turn and map its result through an application-owned validator."""
    config,command,cwd,env=configuration(path)
    timeout=config.get('prompt_timeout',240)
    if type(timeout) not in (int,float) or not 1<=timeout<=600:raise RuntimeErrorCode('ACP_CONFIG_INVALID')
    from .worker_tool_channel import SOCKET_ENV
    from .tool_channel_client import ToolChannelClient
    tool_client=ToolChannelClient(env[SOCKET_ENV]) if env.get(SOCKET_ENV) else None
    tool_description=await tool_client.request('describe',{}) if tool_client else {}
    read_tool=tool_description.get('client_read_tool')
    process=await asyncio.create_subprocess_exec(*command,cwd=cwd,env=env,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=256*1024,
        start_new_session=True)
    peer=AcpClient(process,tool_client=tool_client,client_read_tool=read_tool,permission_decoder=permission_decoder,owns_process_group=True)
    peer.reader=asyncio.create_task(peer.read());peer.stderr=asyncio.create_task(peer.discard_stderr())
    peer.monitor=asyncio.create_task(peer.watch_process())
    loop=asyncio.get_running_loop();task=asyncio.current_task()
    for sig in (signal.SIGTERM,signal.SIGINT):
        try:loop.add_signal_handler(sig,task.cancel)
        except (NotImplementedError,RuntimeError):pass
    result=None
    try:
        initialized=await peer.call('initialize',{'protocolVersion':1,'clientCapabilities':({'fs':{'readTextFile':True,'writeTextFile':False}} if read_tool else {}),
            'clientInfo':{'name':'dios-acp-bridge','version':'0.1'}})
        if initialized.get('protocolVersion')!=1:raise RuntimeErrorCode('ACP_VERSION_UNSUPPORTED')
        method=config.get('authentication_method')
        if method:
            if not any(a.get('id')==method for a in initialized.get('authMethods',[])):
                raise RuntimeErrorCode('ACP_AUTH_METHOD_UNSUPPORTED')
            await peer.call('authenticate',{'methodId':method})
        mcp_servers=[]
        if tool_client:
            mcp_servers=[{'name':'dios','command':sys.executable,
                'args':['-m','app.runtime.worker_mcp_bridge','--socket',env[SOCKET_ENV],'--instance',str(uuid4())],'env':[{'name':'PYTHONPATH','value':str(Path(__file__).resolve().parents[2])}]}]
        session=await peer.call('session/new',{'cwd':str(cwd),'mcpServers':mcp_servers})
        sid=session.get('sessionId')
        if not isinstance(sid,str) or not 1<=len(sid)<=160:raise RuntimeErrorCode('SESSION_INVALID')
        peer.session_id=sid;emit({'type':'session','session_id':sid})
        if tool_client:await tool_client.ready(sid)
        mode=config.get('mode_id')
        if mode:
            modes=session.get('modes',{}).get('availableModes',[])
            if not any(m.get('id')==mode for m in modes):raise RuntimeErrorCode('ACP_MODE_UNSUPPORTED')
            await peer.call('session/set_mode',{'sessionId':sid,'modeId':mode})
        response=await peer.call('session/prompt',{'sessionId':sid,
            'prompt':[{'type':'text','text':prompt}]},timeout=timeout)
        if response.get('stopReason')!='end_turn':raise RuntimeErrorCode('RESULT_INCOMPLETE')
        if peer.native_tool_seen:
            if tool_client is None:raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_UNVERIFIED')
            activity=[]
            for call in peer.tool_activity.values():
                if activity_decoder:call=activity_decoder(call)
                if not isinstance(call.get('name'),str) or not isinstance(call.get('rawInput'),dict):
                    raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_UNVERIFIED')
                activity.append({'name':call['name'],'arguments':call['rawInput']})
            await tool_client.request('verify_activity',{'activity':activity})
        if required_tools:
            if tool_client is None:raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
            await tool_client.request('verify_required',{'requirements':list(required_tools)})
        try:parsed=(output_decoder or json.loads)(peer.output)
        except ValueError:raise RuntimeErrorCode('RESULT_FORMAT_INVALID') from None
        result=decode_result(parsed)
        if len(encoded(result))>60000:raise RuntimeErrorCode('OUTPUT_LIMIT_EXCEEDED')
    finally:
        await peer.close()
        for sig in (signal.SIGTERM,signal.SIGINT):
            try:loop.remove_signal_handler(sig)
            except (NotImplementedError,RuntimeError):pass
    # Completion follows protocol validation and child cleanup; partial outputs remain visible.
    emit({'type':'result','session_id':sid,'result':result})
    # Some persistent ACP servers exit nonzero when stdin closes after end_turn.
    # Retain the validated result; the runtime's explicit acceptance policy decides.
    if process.returncode != 0:diagnostic('ACP_CHILD_EXIT_NONZERO')
    return 0 if process.returncode == 0 else 2

