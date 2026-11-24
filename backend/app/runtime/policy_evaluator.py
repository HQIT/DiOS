"""Thin OPA Data API adapter; decisions include native bundle provenance.

An allow is a decision at query time, not a remote execution reservation.
"""
import asyncio
import ipaddress
import json
import math
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
import httpx
from .database import RuntimeErrorCode
from .store import encoded,identifier,digest

MAX_RESPONSE=65536

class PolicyEvaluator(Protocol):
    async def evaluate(self,configuration:dict,input:dict)->dict: ...

def validate(configuration):
    if not isinstance(configuration,dict) or set(configuration)-{'kind','url','bundles','timeout_seconds','bearer_token_file'}:
        raise RuntimeErrorCode('POLICY_EVALUATOR_CONFIGURATION_INVALID')
    if configuration.get('kind')!='opa':raise RuntimeErrorCode('POLICY_EVALUATOR_UNSUPPORTED')
    url=configuration.get('url')
    if not isinstance(url,str) or len(url)>2048:raise RuntimeErrorCode('POLICY_EVALUATOR_CONFIGURATION_INVALID')
    parts=urlsplit(url)
    try:local=parts.hostname=='localhost' or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:local=False
    if (parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or not parts.path.startswith('/v1/data/') or (parts.scheme=='http' and not local)):
        raise RuntimeErrorCode('POLICY_EVALUATOR_ENDPOINT_INVALID')
    timeout=configuration.get('timeout_seconds',2)
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .05<=timeout<=5:
        raise RuntimeErrorCode('POLICY_EVALUATOR_TIMEOUT_INVALID')
    bundles=configuration.get('bundles')
    if not isinstance(bundles,dict) or not 1<=len(bundles)<=16:raise RuntimeErrorCode('POLICY_BUNDLE_REQUIRED')
    for name,revision in bundles.items():
        if not isinstance(name,str) or not 1<=len(name)<=512:raise RuntimeErrorCode('POLICY_BUNDLE_INVALID')
        identifier(revision)
    if configuration.get('bearer_token_file'):
        if not isinstance(configuration['bearer_token_file'],str) or not Path(configuration['bearer_token_file']).is_absolute():
            raise RuntimeErrorCode('POLICY_EVALUATOR_CREDENTIAL_INVALID')
    return configuration

class OpaEvaluator:
    def __init__(self,*,transport=None):self.transport=transport

    async def evaluate(self,configuration,input):
        validate(configuration)
        payload=encoded({'input':input})
        if len(payload)>MAX_RESPONSE:raise RuntimeErrorCode('POLICY_INPUT_TOO_LARGE')
        headers={'Content-Type':'application/json'}
        if configuration.get('bearer_token_file'):
            def credential():
                p=Path(configuration['bearer_token_file'])
                if p.is_symlink():raise RuntimeErrorCode('POLICY_EVALUATOR_CREDENTIAL_INVALID')
                with p.open('rb') as f:raw=f.read(8193)
                if len(raw)>8192:raise RuntimeErrorCode('POLICY_EVALUATOR_CREDENTIAL_INVALID')
                value=raw.decode().strip()
                if not value or any(ch.isspace() for ch in value):raise RuntimeErrorCode('POLICY_EVALUATOR_CREDENTIAL_INVALID')
                return value
            try:headers['Authorization']='Bearer '+await asyncio.to_thread(credential)
            except (OSError,UnicodeError):raise RuntimeErrorCode('POLICY_EVALUATOR_CREDENTIAL_INVALID') from None
        timeout=configuration.get('timeout_seconds',2)
        try:
            async with asyncio.timeout(timeout),httpx.AsyncClient(timeout=timeout,follow_redirects=False,trust_env=False,transport=self.transport) as client:
                async with client.stream('POST',configuration['url'],params={'provenance':'true'},content=payload,headers=headers) as response:
                    if response.status_code!=200:raise RuntimeErrorCode('POLICY_EVALUATOR_UNAVAILABLE')
                    raw=b''
                    async for chunk in response.aiter_bytes():
                        raw+=chunk
                        if len(raw)>MAX_RESPONSE:raise RuntimeErrorCode('POLICY_RESPONSE_TOO_LARGE')
        except (TimeoutError,httpx.TimeoutException):raise RuntimeErrorCode('POLICY_EVALUATOR_TIMEOUT') from None
        except httpx.HTTPError:raise RuntimeErrorCode('POLICY_EVALUATOR_UNAVAILABLE') from None
        try:value=json.loads(raw)
        except (ValueError,UnicodeError):raise RuntimeErrorCode('POLICY_RESPONSE_INVALID') from None
        if not isinstance(value,dict) or type(value.get('result')) is not bool:raise RuntimeErrorCode('POLICY_RESPONSE_INVALID')
        provenance=value.get('provenance')
        bundles=provenance.get('bundles') if isinstance(provenance,dict) else None
        if not isinstance(bundles,dict):raise RuntimeErrorCode('POLICY_PROVENANCE_REQUIRED')
        actual={name:row.get('revision') for name,row in bundles.items() if isinstance(row,dict)}
        if actual!=configuration['bundles']:raise RuntimeErrorCode('POLICY_REVISION_CHANGED')
        decision_id=value.get('decision_id')
        if decision_id is not None and (not isinstance(decision_id,str) or len(decision_id)>256):raise RuntimeErrorCode('POLICY_RESPONSE_INVALID')
        return {'allow':value['result'],'provider':'opa','bundles':actual,
            'input_digest':digest(input),'decision_id':decision_id}
