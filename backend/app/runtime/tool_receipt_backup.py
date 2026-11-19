"""Pending tool receipts participate in snapshots even before database artifact linking."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from .artifacts import LocalArtifacts
from .database import RuntimeErrorCode
from .store import digest, encoded
from .tool_receipts import ToolReceipts, MAX_RECEIPT, validate_receipt, directory, immutable


def row_value(row):
    value=dict(row)
    for key in ('binding','permission','arguments'):
        if isinstance(value.get(key),str):value[key]=json.loads(value[key])
    return value


def collect(snapshot,artifact_root,budget):
    provider=LocalArtifacts(artifact_root);files={};objects={};sources={}
    db=sqlite3.connect(Path(snapshot).as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        if not db.execute("SELECT name FROM sqlite_master WHERE name='runtime_tool_invocations'").fetchone():
            return {'files':files,'objects':objects},sources
        for row in db.execute("SELECT * FROM runtime_tool_invocations WHERE state IN ('dispatched','result_unknown','succeeded','failed')"):
            budget();item=row_value(row);receipts=ToolReceipts(provider,item['scope'])
            value=receipts.load(item['id'])
            if value is None:continue
            receipts.match(value,item);raw=encoded(value);key=digest(value)
            name=digest(item['scope'])+'-'+digest(item['id'])+'.json'
            files[name]={'sha256':key,'size':len(raw)};sources[name]=receipts.path(item['id'])
            objects[key]={'sha256':key,'size':len(raw)}
            objects[value['output']['key']]={'sha256':value['output']['key'],'size':value['output']['size']}
    finally:db.close()
    return {'files':files,'objects':objects},sources


def verify(snapshot,receipt_dir,object_dir,section):
    if not isinstance(section,dict) or set(section)!={'files','objects'} or not isinstance(section['files'],dict) or not isinstance(section['objects'],dict):
        raise RuntimeErrorCode('BACKUP_TOOL_RECEIPTS_INVALID')
    receipt_dir=Path(receipt_dir);object_dir=Path(object_dir)
    if receipt_dir.is_symlink() or object_dir.is_symlink():raise RuntimeErrorCode('BACKUP_FILE_INVALID')
    objects={};db=sqlite3.connect(Path(snapshot).as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        for name,expected in section['files'].items():
            if not re.fullmatch('[0-9a-f]{64}-[0-9a-f]{64}\.json',name):raise RuntimeErrorCode('BACKUP_TOOL_RECEIPTS_INVALID')
            path=receipt_dir/name
            if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_RECEIPT:raise RuntimeErrorCode('BACKUP_TOOL_RECEIPTS_INVALID')
            raw=path.read_bytes();value=validate_receipt(json.loads(raw.decode('utf-8')))
            if raw!=encoded(value) or expected!={'sha256':digest(value),'size':len(raw)}:raise RuntimeErrorCode('BACKUP_TOOL_RECEIPTS_INVALID')
            context=value['intent']['context']
            if name!=digest(context['scope'])+'-'+digest(context['id'])+'.json':raise RuntimeErrorCode('BACKUP_TOOL_RECEIPTS_INVALID')
            row=db.execute('SELECT * FROM runtime_tool_invocations WHERE scope=? AND id=?',(context['scope'],context['id'])).fetchone()
            if not row:raise RuntimeErrorCode('BACKUP_TOOL_RECEIPTS_INVALID')
            ToolReceipts.match(value,row_value(row))
            key=digest(value);objects[key]={'sha256':key,'size':len(raw)}
            objects[value['output']['key']]={'sha256':value['output']['key'],'size':value['output']['size']}
        if objects!=section['objects']:raise RuntimeErrorCode('BACKUP_TOOL_RECEIPTS_INVALID')
        for key,expected in objects.items():
            path=object_dir/key
            if path.is_symlink() or not path.is_file() or path.stat().st_size!=expected['size']:
                raise RuntimeErrorCode('BACKUP_ARTIFACT_MISMATCH')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=key:raise RuntimeErrorCode('BACKUP_ARTIFACT_MISMATCH')
    finally:db.close()


def install(receipt_dir,provider,section):
    # Called only after full bundle verification, into a fresh restore directory.
    for name in section['files']:
        raw=(Path(receipt_dir)/name).read_bytes();value=validate_receipt(json.loads(raw.decode('utf-8')))
        context=value['intent']['context'];receipts=ToolReceipts(provider,context['scope'])
        directory(receipts.root.parent);directory(receipts.root)
        immutable(receipts.path(context['id'],'intent'),encoded(value['intent']))
        immutable(receipts.path(context['id']),raw)
