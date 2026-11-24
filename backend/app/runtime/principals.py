"""Trusted opaque-token identity provider; application input carries no authority."""
import hashlib
import json
import math
import re
import secrets
import time
from pathlib import Path
from typing import Protocol
from .database import RuntimeErrorCode
from .store import identifier

ACTIONS={'run.submit','run.read','run.cancel','run.revoke','artifact.read','tool.read','tool.approve'}
SHARE_ACTIONS={'run.read','artifact.read','tool.read','tool.approve'}
BINDING_KEYS=('issuer','audience','scope','id','subject','actor','revision')

class PrincipalProvider(Protocol):
    def authenticate(self,token:str,scope:str)->dict: ...
    def current(self,binding:dict)->dict: ...

class FilePrincipals:
    audience='dios-runtime'
    def __init__(self,path):self.path=Path(path)

    def document(self):
        try:
            if not self.path.is_absolute() or self.path.is_symlink():raise ValueError()
            with self.path.open('rb') as f:raw=f.read(262145)
            if len(raw)>262144:raise ValueError()
            value=json.loads(raw)
            if set(value)!={'version','issuer','audience','principals'} or value['version']!=1 or value['audience']!=self.audience:raise ValueError()
            identifier(value['issuer']);rows=value['principals']
            if not isinstance(rows,dict) or len(rows)>256:raise ValueError()
            hashes=set()
            for pid,row in rows.items():
                identifier(pid)
                if set(row)-{'subject','actor','scope','enabled','revision','expires_at','tokens','profiles','objects'}:raise ValueError()
                for k in ('subject','actor','scope'):identifier(row[k])
                if type(row['enabled']) is not bool or type(row['revision']) is not int or row['revision']<1:raise ValueError()
                if type(row['expires_at']) not in (int,float) or not math.isfinite(row['expires_at']):raise ValueError()
                if not isinstance(row['tokens'],list) or not 1<=len(row['tokens'])<=8:raise ValueError()
                for token in row['tokens']:
                    if set(token)!={'sha256','enabled','not_before','expires_at'}:raise ValueError()
                    if not re.fullmatch('[0-9a-f]{64}',token['sha256']) or token['sha256'] in hashes:raise ValueError()
                    hashes.add(token['sha256'])
                    if type(token['enabled']) is not bool:raise ValueError()
                    for k in ('not_before','expires_at'):
                        if type(token[k]) not in (int,float) or not math.isfinite(token[k]):raise ValueError()
                    if token['not_before']>=token['expires_at']:raise ValueError()
                if not isinstance(row['profiles'],dict) or len(row['profiles'])>64:raise ValueError()
                for profile,grant in row['profiles'].items():
                    identifier(profile)
                    if set(grant)!={'actions','permissions'}:raise ValueError()
                    if not isinstance(grant['actions'],list) or not set(grant['actions'])<=ACTIONS:raise ValueError()
                    if not isinstance(grant['permissions'],list) or len(grant['permissions'])>128:raise ValueError()
                    for permission in grant['permissions']:
                        if set(permission)!={'action','resource_kind','resource_id','resource_version','constraint_revision'}:raise ValueError()
                        for item in permission.values():identifier(item)
                objects=row.get('objects',{})
                if not isinstance(objects,dict) or len(objects)>256:raise ValueError()
                for rid,actions in objects.items():
                    identifier(rid)
                    if not isinstance(actions,list) or not set(actions)<=SHARE_ACTIONS:raise ValueError()
            return value
        except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError,RuntimeErrorCode):
            raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None

    def binding(self,doc,pid,row):
        return dict(issuer=doc['issuer'],audience=doc['audience'],id=pid,
            **{k:row[k] for k in ('subject','actor','scope','revision')})

    def authenticate(self,token,scope):
        if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_-]{43,128}',token):
            raise RuntimeErrorCode('AUTH_UNAUTHENTICATED')
        doc=self.document();hashed=hashlib.sha256(token.encode()).hexdigest();now=time.time()
        for pid,row in doc['principals'].items():
            for credential in row['tokens']:
                if secrets.compare_digest(hashed,credential['sha256']):
                    if (row['enabled'] and row['scope']==scope and row['expires_at']>now
                            and credential['enabled'] and credential['not_before']<=now<credential['expires_at']):
                        return self.binding(doc,pid,row)
                    raise RuntimeErrorCode('AUTH_UNAUTHENTICATED')
        raise RuntimeErrorCode('AUTH_UNAUTHENTICATED')

    def current(self,binding):
        doc=self.document()
        try:
            row=doc['principals'][binding['id']]
            if set(binding)!=set(BINDING_KEYS) or binding!=self.binding(doc,binding['id'],row):raise ValueError()
            if not row['enabled'] or row['expires_at']<=time.time():raise ValueError()
            return row
        except (KeyError,TypeError,ValueError):raise RuntimeErrorCode('AUTH_PRINCIPAL_REVOKED') from None


def admit(policy,profile_id,profile_digest,refs,binding=None):
    if binding is None:return policy.admission(profile_id,profile_digest,refs)
    return policy.admission(profile_id,profile_digest,refs,principal=binding)
