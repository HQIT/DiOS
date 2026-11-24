"""RFC 7662 source checks through Authlib; credentials stay on the trusted host."""
import asyncio
import hashlib
import ipaddress
import json
import math
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from .database import RuntimeErrorCode
from .store import identifier,digest

LIMIT=65536

def validate_provider(value):
    required={'issuer','audience','endpoint','client_id'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'timeout_seconds','client_secret_file','client_secret_ref','secret_store'}:
        raise ValueError('oauth2 provider')
    for key in ('issuer','audience','client_id'):identifier(value[key])
    for key in ('issuer','endpoint'):
        u=urlsplit(value[key]);local=u.hostname=='localhost'
        try:local=local or ipaddress.ip_address(u.hostname).is_loopback
        except ValueError:pass
        if not u.hostname or u.username or u.password or u.query or u.fragment or u.scheme not in {'https','http'} or (u.scheme=='http' and not local):raise ValueError('oauth2 endpoint')
        _=u.port
    if ('client_secret_file' in value)==('client_secret_ref' in value):raise ValueError('credential selection')
    if 'client_secret_file' in value and (not isinstance(value['client_secret_file'],str) or not Path(value['client_secret_file']).is_absolute()):raise ValueError('credential path')
    from .openbao_secrets import validate_config,validate_reference as validate_secret
    if 'secret_store' in value:validate_config(value['secret_store'])
    if 'client_secret_ref' in value:
        if 'secret_store' not in value:raise ValueError('secret store required')
        validate_secret(value['client_secret_ref'])
    timeout=value.get('timeout_seconds',2)
    if type(timeout) not in (float,int) or not math.isfinite(timeout) or not .05<=timeout<=5:raise ValueError('timeout')

def validate_reference(value,provider):
    required={'ref','version','sha256','issuer','subject','audience','client_id','expires_at'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'path','secret_ref'}:raise ValueError('source reference')
    if ('path' in value)==('secret_ref' in value):raise ValueError('source selection')
    for key in ('ref','version','issuer','subject','audience','client_id'):identifier(value[key])
    if value['issuer']!=provider['issuer'] or value['audience']!=provider['audience']:raise ValueError('source authority')
    if 'path' in value and (not isinstance(value['path'],str) or not Path(value['path']).is_absolute()):raise ValueError('source path')
    if 'secret_ref' in value:
        from .openbao_secrets import validate_reference as validate_secret
        if 'secret_store' not in provider:raise ValueError('secret store required')
        validate_secret(value['secret_ref'])
    import re
    if not isinstance(value['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['sha256']):raise ValueError('source digest')
    if type(value['expires_at']) not in (int,float) or not math.isfinite(value['expires_at']):raise ValueError('source expiry')

def source_binding(reference,provider):
    return {k:v for k,v in reference.items() if k not in {'path','sha256','secret_ref'}}|{'digest':digest({k:v for k,v in reference.items() if k!='path'}),
        'provider_digest':digest({k:provider[k] for k in ('issuer','audience','endpoint','client_id')} | ({'secret_store':provider['secret_store']['address'].rstrip('/')} if 'secret_store' in provider else {}))}

def private_value(path):
    # This bootstrap Secret reference provider has a POSIX qualification only.
    if os.name!='posix' or not Path(path).is_absolute():raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE')
    descriptor=None
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid!=os.getuid():raise ValueError()
        with os.fdopen(descriptor,'rb') as stream:
            descriptor=None;raw=stream.read(16385)
        if not 1<=len(raw)<=16384:raise ValueError()
        value=raw.decode().strip()
        if not value or not value.isascii() or any(x.isspace() for x in value):raise ValueError()
        return value
    except (OSError,ValueError,UnicodeError):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
    finally:
        if descriptor is not None:os.close(descriptor)

class OAuthSource:
    def __init__(self,provider,*,transport=None,secret_transport=None):self.provider=provider;self.transport=transport;self.secret_transport=secret_transport

    def current(self,reference):
        try:validate_provider(self.provider);validate_reference(reference,self.provider)
        except (ValueError,TypeError,RuntimeErrorCode):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
        if reference['expires_at']<=time.time():raise RuntimeErrorCode('AUTH_PRINCIPAL_EXPIRED')
        try:return asyncio.run(self._current(reference))
        except TimeoutError:raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None

    async def _current(self,reference):
        from .openbao_secrets import OpenBaoSecrets
        async with asyncio.timeout(self.provider.get('timeout_seconds',2)):
            try:
                token=(await OpenBaoSecrets(self.provider['secret_store'],transport=self.secret_transport).read_async(reference['secret_ref'])
                    if 'secret_ref' in reference else private_value(reference['path']))
            except RuntimeErrorCode as exc:
                if exc.code=='SOURCE_SECRET_UNAVAILABLE':raise RuntimeErrorCode('AUTH_PRINCIPAL_REVOKED') from None
                raise
            if hashlib.sha256(token.encode()).hexdigest()!=reference['sha256']:raise RuntimeErrorCode('AUTH_PRINCIPAL_REVOKED')
            try:
                secret=(await OpenBaoSecrets(self.provider['secret_store'],transport=self.secret_transport).read_async(self.provider['client_secret_ref'])
                    if 'client_secret_ref' in self.provider else private_value(self.provider['client_secret_file']))
            except RuntimeErrorCode:raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
            return await self._check(reference,token,secret)

    async def _check(self,reference,token,secret):
        timeout=self.provider.get('timeout_seconds',2)
        try:
            async with asyncio.timeout(timeout),AsyncOAuth2Client(self.provider['client_id'],secret,
                    timeout=timeout,trust_env=False,follow_redirects=False,transport=self.transport) as client:
                # Authlib owns client_secret_basic encoding/authentication. Streaming keeps the response bounded.
                async with client.stream('POST',self.provider['endpoint'],withhold_token=True,
                        auth=client.client_auth('client_secret_basic'),data={'token':token,'token_type_hint':'access_token'},
                        headers={'Accept':'application/json'}) as response:
                    if response.status_code!=200:raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE')
                    raw=b''
                    async for chunk in response.aiter_bytes():
                        raw+=chunk
                        if len(raw)>LIMIT:raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE')
        except (TimeoutError,httpx.HTTPError):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
        try:value=json.loads(raw)
        except (ValueError,UnicodeError):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
        if not isinstance(value,dict) or type(value.get('active')) is not bool:raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE')
        if not value['active']:raise RuntimeErrorCode('AUTH_PRINCIPAL_REVOKED')
        now=time.time();exp=value.get('exp');nbf=value.get('nbf',0)
        audience=value.get('aud');audience=[audience] if isinstance(audience,str) else audience
        if (value.get('iss')!=reference['issuer'] or value.get('sub')!=reference['subject']
                or value.get('client_id')!=reference['client_id'] or not isinstance(audience,list)
                or reference['audience'] not in audience or type(exp) not in (int,float) or not math.isfinite(exp)
                or exp<reference['expires_at'] or exp<=now or type(nbf) not in (int,float) or not math.isfinite(nbf) or nbf>now):
            raise RuntimeErrorCode('AUTH_PRINCIPAL_REVOKED')
        return {'issuer':value['iss'],'subject':value['sub'],'client_id':value['client_id'],'expires_at':reference['expires_at']}
