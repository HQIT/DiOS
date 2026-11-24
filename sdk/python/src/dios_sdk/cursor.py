"""Cursor 2026.11.05 ACP metadata normalization; no execution authority."""
from .acp import execute
from .acp_result import final_object
from .protocol import WorkloadError as RuntimeErrorCode


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


async def cursor_execute(path,prompt,decode_result,**kwargs):
    return await execute(path,prompt,decode_result,activity_decoder=tool_activity,output_decoder=final_object,permission_decoder=proxy_permission,**kwargs)
