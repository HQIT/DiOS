"""Immutable local tool receipts, independent of control database availability.

This is a trusted-host evidence provider, not a remotely authenticated receipt.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from .database import RuntimeErrorCode
from .store import encoded, digest

MAX_RECEIPT=262144
MAX_OUTPUT=1048576
FIELDS=('id','scope','run_id','attempt_id','tool_id','request_digest','registration_digest',
    'binding','permission','arguments_digest','approval_state','approver')


async def durable_io(function,*args,**kwargs):
    pending=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Finish the file commit before propagating cancellation; never abandon an active write.
        await pending
        raise


def directory(path):
    path.mkdir(mode=0o700,exist_ok=True)
    if path.is_symlink() or not path.is_dir():raise RuntimeErrorCode('TOOL_RECEIPT_PATH_INVALID')


def immutable(path,data):
    if path.is_symlink():raise RuntimeErrorCode('TOOL_RECEIPT_PATH_INVALID')
    if path.exists():
        if path.stat().st_size!=len(data) or path.read_bytes()!=data:raise RuntimeErrorCode('TOOL_RECEIPT_CONFLICT')
        return
    fd,temp=tempfile.mkstemp(prefix='.receipt-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
        try:os.link(temp,path)
        except FileExistsError:
            if path.is_symlink() or path.stat().st_size!=len(data) or path.read_bytes()!=data:raise RuntimeErrorCode('TOOL_RECEIPT_CONFLICT')
        if os.name!='nt':
            fd=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:Path(temp).unlink(missing_ok=True)


def validate_receipt(value):
    if not isinstance(value,dict) or set(value)!={'format','intent','kind','reason','output'} or type(value['format']) is not int or value['format']!=1:
        raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    intent=value['intent']
    if not isinstance(intent,dict) or set(intent)!={'context','registration','dispatch_version'}:raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    context=intent['context'];reg=intent['registration']
    if not isinstance(context,dict) or set(context)!=set(FIELDS) or not isinstance(reg,dict):raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    for field in ('id','scope','run_id','attempt_id','tool_id'):
        if not isinstance(context[field],str) or not 1<=len(context[field])<=160:raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    for field in ('request_digest','registration_digest','arguments_digest'):
        key=context[field]
        if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    if not isinstance(context['binding'],dict) or not isinstance(context['permission'],dict) or type(reg.get('approval_required')) is not bool:
        raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    if type(intent['dispatch_version']) is not int or intent['dispatch_version']<1:raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    if digest(reg)!=context['registration_digest'] or reg.get('permission')!=context['permission']:
        raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
    expected='consumed' if reg.get('approval_required') is True else 'not_required'
    if context['approval_state']!=expected:raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
    if value['kind'] not in {'complete','partial'} or not isinstance(value['reason'],str) or len(value['reason'])>160:
        raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    output=value['output']
    if not isinstance(output,dict) or set(output)!={'key','size'}:raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    key=output['key']
    if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    if type(output['size']) is not int or not 0<=output['size']<=MAX_OUTPUT:raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
    return value


class ToolReceipts:
    def __init__(self,provider,scope):
        self.provider,self.scope=provider,scope
        # Merely constructing a query service must not mutate the filesystem.
        self.root=Path(provider.root)/'.tool-receipts-v1'/digest(scope)

    def path(self,call_id,suffix='receipt'):
        if self.root.parent.is_symlink() or self.root.is_symlink():raise RuntimeErrorCode('TOOL_RECEIPT_PATH_INVALID')
        return self.root/(digest(call_id)+'.'+suffix+'.json')

    def available(self,call_id):
        path=self.path(call_id)
        return path.is_file() and not path.is_symlink()

    def prepare(self,item,registration):
        if item['scope']!=self.scope or item['state']!='dispatched':raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
        reg={k:v for k,v in registration.items() if k not in {'id','digest'}}
        value=dict(context={k:item[k] for k in FIELDS},registration=reg,dispatch_version=item['version'])
        if digest(reg)!=item['registration_digest']:raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
        raw=encoded(value)
        if len(raw)>MAX_RECEIPT-2048:raise RuntimeErrorCode('TOOL_RECEIPT_TOO_LARGE')
        directory(self.root.parent);directory(self.root)
        immutable(self.path(item['id'],'intent'),raw)
        return value

    def save(self,intent,data,*,complete,reason):
        if len(data)>MAX_OUTPUT:raise RuntimeErrorCode('TOOL_RESULT_TOO_LARGE')
        if intent['context']['scope']!=self.scope:raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
        output=self.provider.put([data])
        value=validate_receipt(dict(format=1,intent=intent,kind='complete' if complete else 'partial',reason=reason,
            output={'key':output.key,'size':output.size}))
        raw=encoded(value)
        if len(raw)>MAX_RECEIPT:raise RuntimeErrorCode('TOOL_RECEIPT_TOO_LARGE')
        self.provider.put([raw])
        immutable(self.path(intent['context']['id']),raw)
        return value

    def load(self,call_id):
        path=self.path(call_id)
        if not path.exists():return None
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size>MAX_RECEIPT:
            raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
        raw=path.read_bytes()
        try:value=validate_receipt(json.loads(raw.decode('utf-8')))
        except (ValueError,TypeError,KeyError,UnicodeError):raise RuntimeErrorCode('TOOL_RECEIPT_INVALID') from None
        if raw!=encoded(value):raise RuntimeErrorCode('TOOL_RECEIPT_INVALID')
        context=value['intent']['context']
        if context['id']!=call_id or context['scope']!=self.scope:raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
        blob=self.provider.verify(value['output']['key'])
        if blob.size!=value['output']['size']:raise RuntimeErrorCode('TOOL_RECEIPT_OUTPUT_MISMATCH')
        self.provider.verify(hashlib.sha256(raw).hexdigest())
        return value

    @staticmethod
    def match(value,item):
        if value['intent']['context']!={k:item[k] for k in FIELDS} or value['intent']['dispatch_version']>item['version']:
            raise RuntimeErrorCode('TOOL_RECEIPT_BINDING_MISMATCH')
