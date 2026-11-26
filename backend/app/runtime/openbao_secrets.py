"""Read pinned KV v2 credentials and create immutable records through the OpenBao API.

Authentication renewal belongs to the native Agent; this adapter only reads its
private token sink. It never falls back to an unversioned/latest secret.
"""
import asyncio
import ipaddress
import json
import math
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .database import RuntimeErrorCode


def validate_config(value):
    if not isinstance(value,dict) or not {'address','token_file'}<=set(value) or set(value)-{'address','token_file','timeout_seconds','ca_file'}:raise ValueError('secret store')
    if not isinstance(value['address'],str) or not 1<=len(value['address'])<=2048:raise ValueError('secret address')
    u=urlsplit(value['address']);local=u.hostname=='localhost'
    try:local=local or ipaddress.ip_address(u.hostname).is_loopback
    except ValueError:pass
    if not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('','/') or u.scheme not in ('https','http') or (u.scheme=='http' and not local):raise ValueError('secret address')
    _=u.port
    for k in ('token_file','ca_file'):
        if k in value and (not isinstance(value[k],str) or not Path(value[k]).is_absolute()):raise ValueError('secret file')
    timeout=value.get('timeout_seconds',2)
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .05<=timeout<=5:raise ValueError('secret timeout')


def validate_reference(value):
    if not isinstance(value,dict) or set(value)!={'mount','path','version','field'}:raise ValueError('secret reference')
    for k in ('mount','path','field'):
        if not isinstance(value[k],str) or not 1<=len(value[k])<=256:raise ValueError('secret key')
        pattern=r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*' if k!='field' else r'[A-Za-z0-9_-]+'
        if not re.fullmatch(pattern,value[k]):raise ValueError('secret key')
    if type(value['version']) is not int or not 1<=value['version']<=2147483647:raise ValueError('pinned secret version')


class OpenBaoSecrets:
    def __init__(self,config,*,transport=None):self.config=config;self.transport=transport
    def read(self,reference):return asyncio.run(self.read_async(reference))
    async def read_bytes_async(self,reference):
        """Exact UTF-8 field bytes for a native Agent-rendered, versioned file."""
        try:
            value=(await self.read_record_async(reference))[reference['field']]
            if not isinstance(value,str):raise ValueError('secret value')
            raw=value.encode('utf-8')
            if not 1<=len(raw)<=16384:raise ValueError('secret size')
            return raw
        except RuntimeErrorCode:raise
        except (ValueError,TypeError,KeyError,UnicodeError):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
    async def read_async(self,reference):
        try:
            result=(await self.read_record_async(reference))[reference['field']]
            if not isinstance(result,str) or not 1<=len(result)<=16384 or not result.isascii() or any(c.isspace() for c in result):raise ValueError('credential')
            return result
        except RuntimeErrorCode:raise
        except (ValueError,TypeError,KeyError):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None

    async def read_record_async(self,reference):
        from .oauth_source import private_value
        try:validate_config(self.config);validate_reference(reference)
        except (ValueError,TypeError,KeyError):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
        token=private_value(self.config['token_file']);timeout=self.config.get('timeout_seconds',2)
        try:
            verify=ssl.create_default_context(cafile=self.config['ca_file']) if 'ca_file' in self.config else True
            async with asyncio.timeout(timeout),httpx.AsyncClient(timeout=timeout,verify=verify,trust_env=False,follow_redirects=False,transport=self.transport) as client:
                url=f"{self.config['address'].rstrip('/')}/v1/{reference['mount']}/data/{reference['path']}"
                async with client.stream('GET',url,params={'version':reference['version']},headers={'X-Vault-Token':token,'Accept':'application/json'}) as response:
                    if response.status_code==404:raise RuntimeErrorCode('SOURCE_SECRET_UNAVAILABLE')
                    if response.status_code!=200:raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE')
                    raw=b''
                    async for chunk in response.aiter_bytes():
                        raw+=chunk
                        if len(raw)>65536:raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE')
            value=json.loads(raw)['data'];meta=value['metadata']
            if type(meta['version']) is not int or meta['version']!=reference['version']:raise ValueError('version')
            if type(meta['destroyed']) is not bool or not isinstance(meta['deletion_time'],str):raise ValueError('metadata')
            deleted_at=datetime.fromisoformat(meta['deletion_time'].replace('Z','+00:00')) if meta['deletion_time'] else None
            if deleted_at is not None and deleted_at.tzinfo is None:raise ValueError('deletion timezone')
            if meta['destroyed'] or (deleted_at is not None and deleted_at<=datetime.now(timezone.utc)):raise RuntimeErrorCode('SOURCE_SECRET_UNAVAILABLE')
            if not isinstance(value['data'],dict):raise TypeError('record')
            return value['data']
        except RuntimeErrorCode:raise
        except (TimeoutError,httpx.HTTPError,OSError,ValueError,TypeError,KeyError,RecursionError):
            raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None


    async def create_once_async(self,reference,record):
        """Native KV CAS=0; retries read version 1 and never overwrite a tombstone."""
        from .oauth_source import private_value
        try:
            validate_config(self.config);validate_reference(reference)
            if reference['version']!=1 or not isinstance(record,dict) or len(json.dumps(record).encode())>32768:raise ValueError('record')
        except (ValueError,TypeError,KeyError):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
        try:return await self.read_record_async(reference)
        except RuntimeErrorCode as exc:
            if exc.code!='SOURCE_SECRET_UNAVAILABLE':raise
        token=private_value(self.config['token_file']);timeout=self.config.get('timeout_seconds',2)
        try:
            verify=ssl.create_default_context(cafile=self.config['ca_file']) if 'ca_file' in self.config else True
            async with asyncio.timeout(timeout),httpx.AsyncClient(timeout=timeout,verify=verify,trust_env=False,follow_redirects=False,transport=self.transport) as client:
                url=f"{self.config['address'].rstrip('/')}/v1/{reference['mount']}/data/{reference['path']}"
                # No response body is trusted; an uncertain result is recovered by reading the pinned record.
                async with client.stream('POST',url,json={'options':{'cas':0},'data':record},headers={'X-Vault-Token':token,'Accept':'application/json'}):
                    # A failed write reply may still follow a committed write. Only readback admits it.
                    pass
        except (TimeoutError,httpx.HTTPError):pass
        except (OSError,ValueError):raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
        try:return await self.read_record_async(reference)
        except RuntimeErrorCode:raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE') from None
