"""Certificate allowlist for an already TLS-authenticated Worker connection."""
import hashlib
import json
import math
import secrets
from pathlib import Path
import re
import ssl
import time
from .database import RuntimeErrorCode
from .store import identifier

PROTOCOL='dios-worker-tools/1'


def document(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>262144:raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
    try:value=json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,UnicodeError):raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID') from None
    if not isinstance(value,dict) or value.get('version')!=1:raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
    return value


def tls_context(ca,certificate,key,*,server=False):
    # Explicit contexts avoid environment-enabled TLS session-key logging.
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version=ssl.TLSVersion.TLSv1_2
    ctx.verify_mode=ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=ca);ctx.load_cert_chain(certificate,key)
    ctx.set_alpn_protocols([PROTOCOL])
    return ctx


class WorkerIdentities:
    def __init__(self,path,audience):self.path,self.audience=Path(path),audience;identifier(audience)

    def machine(self,fingerprint,scope,profile,worker_id,*,now=None):
        config=document(self.path)
        if config.get('audience')!=self.audience:raise RuntimeErrorCode('WORKER_IDENTITY_SCOPE_DENIED')
        rows=config.get('machines',[])
        if not isinstance(rows,list) or len(rows)>1024:raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
        matches=[row for row in rows if isinstance(row,dict) and row.get('certificate_sha256')==fingerprint]
        if len(matches)!=1:raise RuntimeErrorCode('WORKER_IDENTITY_DENIED')
        row=matches[0];now=time.time() if now is None else now
        for key in ('not_before','expires_at'):
            if type(row.get(key)) not in (int,float) or not math.isfinite(row[key]):raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
        if row.get('enabled') is not True or row.get('issuance_enabled') is not True or not row['not_before']<=now<row['expires_at']:
            raise RuntimeErrorCode('WORKER_IDENTITY_EXPIRED_OR_REVOKED')
        if type(row.get('revision')) is not int or row['revision']<1:raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
        identifier(row.get('subject'));identifier(row.get('worker_id'))
        if {'scope':scope,'profile':profile} not in row.get('bindings',[]) or not (worker_id==row['worker_id'] or re.fullmatch(re.escape(row['worker_id'])+r':[0-9a-f]{32}',worker_id)):
            raise RuntimeErrorCode('WORKER_IDENTITY_SCOPE_DENIED')
        from .store import digest
        return {**row,'machine_digest':digest(row)}

    def authorize(self,fingerprint,scope,profile,worker_id,token,*,now=None):
        config=document(self.path);rows=config.get('workers')
        if config.get('audience')!=self.audience or not isinstance(rows,list) or len(rows)>1024:
            raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
        if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_-]{43,128}',token):raise RuntimeErrorCode('WORKER_TOKEN_DENIED')
        token_hash=hashlib.sha256(token.encode()).hexdigest()
        match=[];seen=set()
        for row in rows:
            if not isinstance(row,dict):raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
            hashed=row.get('certificate_sha256','')
            token_digest=row.get('token_sha256','')
            if (not isinstance(hashed,str) or not re.fullmatch('[0-9a-f]{64}',hashed)
                    or not isinstance(token_digest,str) or not re.fullmatch('[0-9a-f]{64}',token_digest) or (hashed,token_digest) in seen):
                raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
            seen.add((hashed,token_digest))
            if hashed==fingerprint and secrets.compare_digest(token_hash,token_digest):match.append(row)
        if len(match)!=1:raise RuntimeErrorCode('WORKER_IDENTITY_DENIED')
        row=match[0];now=time.time() if now is None else now
        for key in ('not_before','expires_at'):
            if type(row.get(key)) not in (float,int) or not math.isfinite(row[key]):raise RuntimeErrorCode('WORKER_IDENTITY_CONFIG_INVALID')
        if not 0<row['expires_at']-row['not_before']<=900:raise RuntimeErrorCode('WORKER_TOKEN_LIFETIME_INVALID')
        if row.get('enabled') is not True or not row['not_before']<=now<row['expires_at']:
            raise RuntimeErrorCode('WORKER_IDENTITY_EXPIRED_OR_REVOKED')
        identifier(row.get('subject'));identifier(row.get('worker_id'))
        bindings=row.get('bindings')
        if not isinstance(bindings,list) or {'scope':scope,'profile':profile} not in bindings or not (worker_id==row['worker_id'] or re.fullmatch(re.escape(row['worker_id'])+r':[0-9a-f]{32}',worker_id)):
            raise RuntimeErrorCode('WORKER_IDENTITY_SCOPE_DENIED')
        return {'subject':row['subject'],'certificate_sha256':fingerprint,'worker_id':worker_id,'audience':self.audience}


def peer_identity(writer):
    tls=writer.get_extra_info('ssl_object')
    if tls is None or tls.selected_alpn_protocol()!=PROTOCOL:raise RuntimeErrorCode('WORKER_TLS_REQUIRED')
    certificate=tls.getpeercert(binary_form=True);details=tls.getpeercert()
    if not certificate or not details:raise RuntimeErrorCode('WORKER_TLS_REQUIRED')
    return hashlib.sha256(certificate).hexdigest(),ssl.cert_time_to_seconds(details['notAfter'])
