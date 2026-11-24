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
            if set(value)-{'version','issuer','audience','principals','oauth2','oauth_sources','oauth_intake'} or not {'version','issuer','audience','principals'}<=set(value) or value['version']!=1 or value['audience']!=self.audience:raise ValueError()
            identifier(value['issuer']);rows=value['principals']
            if 'oauth2' in value:
                from .oauth_source import validate_provider
                validate_provider(value['oauth2'])
            if not isinstance(rows,dict) or len(rows)>256:raise ValueError()
            if 'oauth_intake' in value:
                from .source_intake import validate_config as validate_intake
                validate_intake(value['oauth_intake'],value['oauth2'])
            hashes=set();versions=set();owners=set();mappings=set()
            sources=value.get('oauth_sources',{})
            if not isinstance(sources,dict) or len(sources)>1024:raise ValueError()
            def validate_source(source):
                from .oauth_source import validate_reference
                validate_reference(source,value['oauth2'])
                version=(source['issuer'],source['ref'],source['version'])
                if source['sha256'] in hashes or version in versions:raise ValueError()
                hashes.add(source['sha256']);versions.add(version)
            for source_id,source in sources.items():
                identifier(source_id);validate_source(source)
            for pid,row in rows.items():
                identifier(pid)
                if set(row)-{'subject','actor','scope','enabled','revision','expires_at','tokens','profiles','objects','upstream','source_refs','oauth_identity'}:raise ValueError()
                for k in ('subject','actor','scope'):identifier(row[k])
                if type(row['enabled']) is not bool or type(row['revision']) is not int or row['revision']<1:raise ValueError()
                if type(row['expires_at']) not in (int,float) or not math.isfinite(row['expires_at']):raise ValueError()
                if 'oauth_identity' in row:
                    from .source_intake import validate_identity
                    if 'oauth_intake' not in value or 'source_refs' not in row:raise ValueError()
                    validate_identity(row['oauth_identity'],value['oauth2'])
                    mapping=tuple([row['scope']]+[row['oauth_identity'][k] for k in ('issuer','subject','client_id')])
                    if mapping in mappings:raise ValueError()
                    mappings.add(mapping)
                if 'upstream' in row:validate_source(row['upstream'])
                if 'source_refs' in row:
                    refs=row['source_refs']
                    if 'upstream' in row or not isinstance(refs,list) or len(refs)>64 or row['tokens']!=[]:raise ValueError()
                    identity=set()
                    for ref in refs:
                        identifier(ref)
                        if ref not in sources or ref in owners:raise ValueError()
                        owners.add(ref);source=sources[ref]
                        identity.add((source['issuer'],source['subject'],source['client_id']))
                    if 'oauth_identity' in row:identity.add(tuple(row['oauth_identity'][k] for k in ('issuer','subject','client_id')))
                    if len(identity)>1:raise ValueError()
                if not isinstance(row['tokens'],list) or not (0 if 'upstream' in row or 'source_refs' in row else 1)<=len(row['tokens'])<=8:raise ValueError()
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

    @staticmethod
    def sources(doc,row):
        if 'upstream' in row:return [row['upstream']]
        return [doc['oauth_sources'][ref] for ref in row.get('source_refs',[])]

    def binding(self,doc,pid,row,source=None):
        from .oauth_source import source_binding
        source=row.get('upstream') if source is None else source
        return dict(issuer=doc['issuer'],audience=doc['audience'],id=pid,
            **({'upstream':source_binding(source,doc['oauth2'])} if source is not None else {}),
            **{k:row[k] for k in ('subject','actor','scope','revision')})

    def authenticate(self,token,scope):
        if not isinstance(token,str) or not 1<=len(token)<=16384 or not token.isascii() or any(c.isspace() for c in token):
            raise RuntimeErrorCode('AUTH_UNAUTHENTICATED')
        doc=self.document();hashed=hashlib.sha256(token.encode()).hexdigest();now=time.time()
        for pid,row in doc['principals'].items():
            for source in self.sources(doc,row):
                if secrets.compare_digest(source['sha256'],hashed):
                    if row['scope']!=scope:raise RuntimeErrorCode('AUTH_UNAUTHENTICATED')
                    binding=self.binding(doc,pid,row,source);self.current(binding);return binding
            for credential in row['tokens']:
                if not re.fullmatch('[A-Za-z0-9_-]{43,128}',token):continue
                if secrets.compare_digest(hashed,credential['sha256']):
                    if (row['enabled'] and row['scope']==scope and row['expires_at']>now
                            and credential['enabled'] and credential['not_before']<=now<credential['expires_at']):
                        binding=self.binding(doc,pid,row);self.current(binding);return binding
                    raise RuntimeErrorCode('AUTH_UNAUTHENTICATED')
        if 'oauth_intake' in doc:
            from .source_intake import SourceIntake
            pid,source=SourceIntake(doc).authenticate(token,scope)
            binding=self.binding(doc,pid,doc['principals'][pid],source);self.current(binding);return binding
        raise RuntimeErrorCode('AUTH_UNAUTHENTICATED')

    def current(self,binding):
        doc=self.document()
        try:
            row=doc['principals'][binding['id']]
            if not row['enabled'] or row['expires_at']<=time.time():raise ValueError()
            source=None
            if 'upstream' in row or 'source_refs' in row:
                from .oauth_source import source_binding
                source=next((ref for ref in self.sources(doc,row)
                    if source_binding(ref,doc['oauth2'])==binding.get('upstream')),None)
                if source is None and 'oauth_identity' in row and isinstance(binding.get('upstream'),dict):
                    from .source_intake import SourceIntake
                    source=SourceIntake(doc).resolve(binding['id'],row,binding['upstream'])
                if source is None:raise ValueError()
            if binding!=self.binding(doc,binding['id'],row,source):raise ValueError()
            if not row['enabled'] or row['expires_at']<=time.time():raise ValueError()
            if source is not None:
                from .oauth_source import OAuthSource
                OAuthSource(doc['oauth2']).current(source)
                # Other sessions' metadata and lifetimes are not part of this work's authority.
                return dict(row,expires_at=min(row['expires_at'],source['expires_at']))
            return row
        except RuntimeErrorCode:raise
        except (KeyError,TypeError,ValueError):raise RuntimeErrorCode('AUTH_PRINCIPAL_REVOKED') from None


def admit(policy,profile_id,profile_digest,refs,binding=None):
    if binding is None:return policy.admission(profile_id,profile_digest,refs)
    return policy.admission(profile_id,profile_digest,refs,principal=binding)
