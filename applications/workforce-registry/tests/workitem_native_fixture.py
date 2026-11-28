"""Isolated synthetic resource: JSONL reservation and ACP compensating release."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

STATE=Path('/run/workitem-fixture')


def emit(value):print(json.dumps(value,ensure_ascii=False),flush=True)


async def forward():
    sid='forward-'+uuid4().hex
    emit(dict(type='session',session_id=sid))
    marker=STATE/'network-fault-used'
    if not marker.exists():
        await asyncio.sleep(1)  # Keep the failed process observable before its explicit exit.
        emit(dict(type='error',code='PROVIDER_NETWORK_ERROR'));return 2
    from app.runtime.tool_channel_client import ToolChannelClient
    channel=ToolChannelClient(os.environ['DIOS_TOOL_SOCKET'])
    await channel.ready(sid)
    await channel.call('reserve',{'resource':'fixture-reservation'},'reserve-once')
    emit(dict(type='result',session_id=sid,result=dict(summary='synthetic reservation',evidence=['fixture:reservation'])))
    return 0


def server():
    sid='undo-'+uuid4().hex
    for line in sys.stdin:
        message=json.loads(line)
        if 'id' not in message:continue
        method=message.get('method')
        if method=='initialize':result={'protocolVersion':1}
        elif method=='session/new':result={'sessionId':sid}
        elif method=='session/prompt':
            from app.runtime.tool_channel_client import ToolChannelClient
            async def release():
                channel=ToolChannelClient(os.environ['DIOS_TOOL_SOCKET'])
                await channel.ready(sid)
                return await channel.call('release',{'resource':'fixture-reservation'},'release-once')
            asyncio.run(release())
            emit(dict(jsonrpc='2.0',method='session/update',params=dict(sessionId=sid,update=dict(
                sessionUpdate='tool_call',toolCallId='release',name='fixture.release',rawInput={'resource':'fixture-reservation'}))))
            emit(dict(jsonrpc='2.0',method='session/update',params=dict(sessionId=sid,update=dict(
                sessionUpdate='agent_message_chunk',content=dict(type='text',text=json.dumps(dict(
                    summary='synthetic release',evidence=['fixture:release-receipt'],server_pid=os.getpid())))))))
            result={'stopReason':'end_turn'}
        else:raise ValueError(method)
        emit(dict(jsonrpc='2.0',id=message['id'],result=result))


async def bridge():
    from app.runtime.acp_bridge import execute
    with tempfile.TemporaryDirectory() as directory:
        config=Path(directory)/'config.json'
        config.write_text(json.dumps(dict(command=[sys.executable,__file__,'server'],cwd=directory,prompt_timeout=20)))
        return await execute(config,'release the registered synthetic reservation',lambda value:value)


if __name__=='__main__':
    if sys.argv[1]=='server':server()
    else:raise SystemExit(asyncio.run(forward() if sys.argv[1]=='forward' else bridge()))
