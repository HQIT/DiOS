"""Cursor 2026.11.05 ACP metadata normalization; no execution authority."""
import json
from .acp_bridge import execute
from .database import RuntimeErrorCode


def tool_activity(call):
    raw=call.get('rawInput')
    if (call.get('status') not in {'completed','failed'} or not isinstance(raw,dict) or
            set(raw)!={'providerIdentifier','toolName','args'} or raw['providerIdentifier']!='dios' or
            not isinstance(raw['toolName'],str) or not isinstance(raw['args'],dict)):
        raise RuntimeErrorCode('ACP_TOOL_ACTIVITY_UNVERIFIED')
    return {'name':'mcp__dios__'+raw['toolName'],'rawInput':raw['args']}


def proxy_permission(call):
    raw=call.get('rawInput')
    if (not isinstance(raw,dict) or set(raw)!={'providerIdentifier','toolName','args'} or
            raw['providerIdentifier']!='dios' or not isinstance(raw['toolName'],str) or not isinstance(raw['args'],dict)):
        raise RuntimeErrorCode('ACP_NATIVE_OPERATION_DENIED')
    return {'name':'mcp__dios__'+raw['toolName'],'arguments':raw['args']}


def final_object(text):
    # ACP streams commentary and the final answer through the same channel.
    # Keep the raw stream as evidence, decode only a complete final JSON object.
    text=text.rstrip()
    if text.endswith('```'):text=text[:-3].rstrip()
    decoder=json.JSONDecoder()
    for offset in range(len(text)-1,-1,-1):
        if text[offset]!='{':continue
        try:value,end=decoder.raw_decode(text,offset)
        except ValueError:continue
        if end==len(text) and isinstance(value,dict):return value
    raise ValueError('No complete final JSON object')


async def cursor_execute(path,prompt,decode_result,**kwargs):
    return await execute(path,prompt,decode_result,activity_decoder=tool_activity,output_decoder=final_object,permission_decoder=proxy_permission,**kwargs)
