"""Operator-owned registered endpoints and separately authenticated approvers."""
import hashlib
import json
import secrets
import re
from pathlib import Path
from urllib.parse import urlsplit
from jsonschema import Draft202012Validator
from .database import RuntimeErrorCode
from .store import digest, identifier


def document(path):
    path=Path(path)
    if path.stat().st_size>131072:raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or value.get('version')!=1:raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
    return value


class FileToolCatalog:
    def __init__(self,path):self.path=Path(path)

    def registration(self,tool_id):
        config=document(self.path);rows=config.get('tools',{})
        if not isinstance(rows,dict):raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
        row=rows.get(tool_id)
        if not isinstance(row,dict) or row.get('enabled') is not True:raise RuntimeErrorCode('TOOL_NOT_AVAILABLE')
        fields={'enabled','transport','url','name','permission','approval_required','input_schema','result_schema','credential_ref'}
        if set(row)-fields or row.get('transport')!='mcp-http':raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
        if not isinstance(row.get('url'),str):raise RuntimeErrorCode('TOOL_ENDPOINT_INVALID')
        url=urlsplit(row['url'])
        if url.scheme not in {'http','https'} or not url.hostname or url.username or url.password or url.fragment or url.query:
            raise RuntimeErrorCode('TOOL_ENDPOINT_INVALID')
        identifier(row.get('name'))
        if type(row.get('approval_required')) is not bool:raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
        # Keep unbounded schema references and network retrieval out of this provider.
        def local_schema(value):
            if isinstance(value,dict):
                if any(k in value for k in ('$ref','$dynamicRef')):raise RuntimeErrorCode('TOOL_SCHEMA_REFERENCE_UNSUPPORTED')
                for v in value.values():local_schema(v)
            elif isinstance(value,list):
                for v in value:local_schema(v)
        for key in ('input_schema','result_schema'):
            if not isinstance(row.get(key),dict):raise RuntimeErrorCode('TOOL_SCHEMA_REQUIRED')
            local_schema(row[key])
            try:Draft202012Validator.check_schema(row[key])
            except Exception:raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID') from None
        if not isinstance(row.get('permission'),dict):raise RuntimeErrorCode('TOOL_PERMISSION_INVALID')
        implementation={k:v for k,v in row.items() if k not in {'permission','enabled'}}
        if row['permission'].get('resource_version')!=digest(implementation):raise RuntimeErrorCode('TOOL_VERSION_NOT_PINNED')
        return {**row,'id':tool_id,'digest':digest(row)}


class FileApprovers:
    """Local provider: token hash is mapped to identity and scope on the server."""
    def __init__(self,path):self.path=Path(path)

    def rows(self):
        rows=document(self.path).get('approvers',[])
        if not isinstance(rows,list):raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
        subjects=set();hashes=set()
        for row in rows:
            if not isinstance(row,dict):raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
            identifier(row.get('subject'))
            hashed=row.get('token_sha256')
            if row.get('enabled') is not True:continue
            if not isinstance(hashed,str) or not re.fullmatch('[0-9a-f]{64}',hashed):raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
            for key in ('scopes','tools'):
                if not isinstance(row.get(key),list) or not row[key]:raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
                for item in row[key]:identifier(item)
            if row['subject'] in subjects or hashed in hashes:raise RuntimeErrorCode('TOOL_CONFIGURATION_INVALID')
            subjects.add(row['subject']);hashes.add(hashed)
        return rows

    def authenticate(self,token,scope,tool_id=None):
        if not token:raise RuntimeErrorCode('APPROVER_UNAUTHENTICATED')
        hashed=hashlib.sha256(token.encode()).hexdigest()
        rows=self.rows()
        matches=[]
        for row in rows:
            if (isinstance(row,dict) and isinstance(row.get('token_sha256'),str)
                    and secrets.compare_digest(hashed,row['token_sha256'])):matches.append(row)
        if len(matches)!=1:raise RuntimeErrorCode('APPROVER_UNAUTHENTICATED')
        row=matches[0];identifier(row.get('subject'))
        if row.get('enabled') is not True or scope not in row.get('scopes',[]):raise RuntimeErrorCode('APPROVER_FORBIDDEN')
        if tool_id is not None and tool_id not in row.get('tools',[]):raise RuntimeErrorCode('APPROVER_FORBIDDEN')
        return row['subject']

    def eligible(self,subject,scope,tool_id):
        rows=self.rows()
        matches=[r for r in rows if isinstance(r,dict) and r.get('subject')==subject and r.get('enabled') is True
            and scope in r.get('scopes',[]) and tool_id in r.get('tools',[])]
        if len(matches)!=1:raise RuntimeErrorCode('APPROVER_FORBIDDEN')
