"""Short-lived sender-bound credentials, with durable rotation and idempotent recovery."""
import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
import os
from pathlib import Path
from sqlalchemy import select
from .credential_schema import credentials as rows, require
from .database import RuntimeErrorCode
from .store import digest,encoded
from .work_authorizations import checked_work,audit
from app.services.e2ag import Decision


def token_hash(token):
    if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_-]{43}',token):raise RuntimeErrorCode('WORKER_TOKEN_DENIED')
    return hashlib.sha256(token.encode()).hexdigest()


class WorkerCredentials:
    def __init__(self,store,identities,policy,key_file,*,ttl=900):
        if type(ttl) is not int or not 60<=ttl<=900:raise RuntimeErrorCode('WORKER_TOKEN_LIFETIME_INVALID')
        self.store,self.identities,self.policy=store,identities,policy
        self.key_file=Path(key_file);self.ttl=ttl

    def key(self):
        path=self.key_file
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=32:raise RuntimeErrorCode('CREDENTIAL_ISSUER_KEY_INVALID')
        if os.name=='posix' and path.stat().st_mode & 0o077:raise RuntimeErrorCode('CREDENTIAL_ISSUER_KEY_PERMISSIONS')
        key=path.read_bytes();return key,hashlib.sha256(key).hexdigest()

    @staticmethod
    def derive(key,identifier,version,nonce):
        return base64.urlsafe_b64encode(hmac.digest(key,b'dios-worker-credential-v1\0'+encoded([identifier,version,nonce]),'sha256')).rstrip(b'=').decode()

    async def context(self,c,fingerprint,cert_expiry,envelope):
        now=await self.store.db.now(c)
        if envelope['scope']!=self.store.scope or envelope['audience']!=self.identities.audience:raise RuntimeErrorCode('WORKER_IDENTITY_SCOPE_DENIED')
        if cert_expiry<=max(now,time.time()):raise RuntimeErrorCode('WORKER_CERTIFICATE_EXPIRED')
        binding=envelope['binding']
        machine=await asyncio.to_thread(self.identities.machine,fingerprint,self.store.scope,envelope['profile'],binding[2],now=now)
        run,attempt,_=await self.store._owned(c,*binding,authorize=True)
        work=await checked_work(c,run,attempt,now,'execute')
        if not work or run['input']['runtime_profile']!=envelope['profile'] or run['input']['profile_digest']!=envelope['profile_digest']:
            raise RuntimeErrorCode('WORKER_CREDENTIAL_BINDING_CHANGED')
        current=await asyncio.to_thread(self.policy.admission,envelope['profile'],envelope['profile_digest'],run['input'].get('secret_refs',[]))
        if current['policy_digest']!=work['policy_digest']:raise RuntimeErrorCode('WORK_POLICY_CHANGED')
        authority={'machine_digest':machine['machine_digest'],'certificate_sha256':fingerprint,'subject':machine['subject'],
            'audience':self.identities.audience,'profile':envelope['profile'],'profile_digest':envelope['profile_digest'],
            'authorization_id':work['id'],'authorization_epoch':attempt['authorization_epoch'],
            'policy_digest':work['policy_digest'],'permissions_digest':digest(work['permissions'])}
        return authority,min(now+self.ttl,cert_expiry,machine['expires_at'],work['expires_at']),now

    def identifier(self,fingerprint,envelope):
        return digest([self.store.scope,self.identities.audience,fingerprint,envelope['binding']])

    async def exchange(self,fingerprint,cert_expiry,envelope):
        method=envelope['message']['method'];params=envelope['message'].get('params')
        expected={'nonce'} if method=='credentials.issue' else {'nonce','expected_version'}
        if not isinstance(params,dict) or set(params)!=expected or not isinstance(params.get('nonce'),str) or not re.fullmatch('[A-Za-z0-9_-]{43}',params['nonce']):
            raise RuntimeErrorCode('WORKER_CREDENTIAL_REQUEST_INVALID')
        if method!='credentials.issue' and (type(params['expected_version']) is not int or params['expected_version']<1):raise RuntimeErrorCode('INVALID_VERSION')
        key,key_id=await asyncio.to_thread(self.key)
        identifier=self.identifier(fingerprint,envelope);nonce=params['nonce']
        async with self.store.db.transaction(self.store.scope) as c:
            await require(c);authority,deadline,now=await self.context(c,fingerprint,cert_expiry,envelope)
            old=(await c.execute(select(rows).where(rows.c.id==identifier,rows.c.scope==self.store.scope))).mappings().first()
            if old:
                if old['state']!='active':raise RuntimeErrorCode('WORKER_CREDENTIAL_REVOKED')
                if old['binding']!=envelope['binding'] or old['authority']!=authority:raise RuntimeErrorCode('WORKER_CREDENTIAL_BINDING_CHANGED')
                if old['key_id']!=key_id:raise RuntimeErrorCode('CREDENTIAL_ISSUER_KEY_CHANGED')
                # A retried exchange reproduces its original secret without storing plaintext.
                if old['nonce']==nonce and old['expires_at']>now:
                    if method=='credentials.renew':
                        supplied=token_hash(envelope['access_token'])
                        if params['expected_version']!=old['version']-1 or not secrets.compare_digest(supplied,old['previous_hash'] or ''):
                            raise RuntimeErrorCode('WORKER_TOKEN_DENIED')
                    token=self.derive(key,identifier,old['version'],nonce)
                    if token_hash(token)!=old['token_hash']:raise RuntimeErrorCode('CREDENTIAL_ISSUER_KEY_CHANGED')
                    return self.response(old,token,now)
                if method=='credentials.issue':
                    if old['expires_at']>now:raise RuntimeErrorCode('WORKER_CREDENTIAL_ALREADY_ISSUED')
                else:
                    if old['expires_at']<=now:raise RuntimeErrorCode('WORKER_CREDENTIAL_EXPIRED')
                    if params['expected_version']!=old['version']:raise RuntimeErrorCode('VERSION_CONFLICT')
                    if not secrets.compare_digest(token_hash(envelope['access_token']),old['token_hash']):raise RuntimeErrorCode('WORKER_TOKEN_DENIED')
            elif method!='credentials.issue':raise RuntimeErrorCode('WORKER_CREDENTIAL_NOT_FOUND')
            if deadline<=now:raise RuntimeErrorCode('WORKER_CREDENTIAL_EXPIRED')
            version=old['version']+1 if old else 1
            token=self.derive(key,identifier,version,nonce)
            value=dict(id=identifier,scope=self.store.scope,run_id=envelope['binding'][0],attempt_id=envelope['binding'][1],
                binding=envelope['binding'],authority=authority,key_id=key_id,state='active',version=version,nonce=nonce,
                token_hash=token_hash(token),previous_hash=old['token_hash'] if old else None,
                previous_until=min(now+30,old['expires_at']) if old else None,expires_at=deadline,
                created_at=old['created_at'] if old else now,updated_at=now)
            if old:await c.execute(rows.update().where(rows.c.id==identifier).values(**value))
            else:await c.execute(rows.insert().values(**value))
            await audit(c,self.store.scope,value['run_id'],value['attempt_id'],method,
                Decision('worker-credential','allow',('WORKER_CREDENTIAL_ROTATED' if old else 'WORKER_CREDENTIAL_ISSUED',),
                    evidence={'credential_id':identifier,'version':version,'certificate_sha256':fingerprint,'expires_at':deadline}),now)
        return self.response(value,token,now)

    @staticmethod
    def response(row,token,now):
        return {'credential_id':row['id'],'version':row['version'],'access_token':token,'expires_at':row['expires_at'],
            'server_time':now,'renew_after_seconds':max(1,(row['expires_at']-now)*2/3)}

    async def authorize(self,fingerprint,cert_expiry,envelope,connection=None):
        if connection is not None:return await self._authorize(connection,fingerprint,cert_expiry,envelope)
        async with self.store.db.read(self.store.scope) as c:
            return await self._authorize(c,fingerprint,cert_expiry,envelope)

    async def _authorize(self,c,fingerprint,cert_expiry,envelope):
        supplied=token_hash(envelope['access_token']);identifier=self.identifier(fingerprint,envelope)
        await require(c);authority,_,now=await self.context(c,fingerprint,cert_expiry,envelope)
        row=(await c.execute(select(rows).where(rows.c.scope==self.store.scope,rows.c.id==identifier))).mappings().first()
        if not row:raise RuntimeErrorCode('WORKER_CREDENTIAL_NOT_FOUND')
        if row['state']!='active':raise RuntimeErrorCode('WORKER_CREDENTIAL_REVOKED')
        if row['authority']!=authority:raise RuntimeErrorCode('WORKER_CREDENTIAL_BINDING_CHANGED')
        if row['expires_at']<=now:raise RuntimeErrorCode('WORKER_CREDENTIAL_EXPIRED')
        current=secrets.compare_digest(supplied,row['token_hash'])
        previous=row['previous_until'] and row['previous_until']>now and secrets.compare_digest(supplied,row['previous_hash'] or '')
        if not (current or previous):raise RuntimeErrorCode('WORKER_TOKEN_DENIED')
        return {'subject':authority['subject'],'certificate_sha256':fingerprint,'worker_id':envelope['binding'][2],
            'audience':authority['audience'],'credential_id':identifier,'credential_version':row['version']}

    async def revoke(self,identifier,version):
        async with self.store.db.transaction(self.store.scope) as c:
            from .lifecycle import admission
            await admission(c,self.store.scope,ownership=True);await require(c)
            row=(await c.execute(select(rows).where(rows.c.id==identifier,rows.c.scope==self.store.scope))).mappings().first()
            if not row:raise RuntimeErrorCode('NOT_FOUND')
            if row['state']=='revoked':return {'id':identifier,'state':'revoked','version':row['version']}
            if row['version']!=version:raise RuntimeErrorCode('VERSION_CONFLICT')
            now=await self.store.db.now(c)
            await c.execute(rows.update().where(rows.c.id==identifier).values(state='revoked',version=version+1,updated_at=now))
            await audit(c,self.store.scope,row['run_id'],row['attempt_id'],'credentials.revoke',
                Decision('worker-credential','deny',('WORKER_CREDENTIAL_REVOKED',),evidence={'credential_id':identifier}),now)
        return {'id':identifier,'state':'revoked','version':version+1}

    async def status(self,run_id):
        async with self.store.db.read(self.store.scope) as c:
            await self.store._run(c,run_id);await require(c)
            result=(await c.execute(select(rows).where(rows.c.scope==self.store.scope,rows.c.run_id==run_id).limit(100))).mappings().all()
            now=await self.store.db.now(c)
        return {'items':[{k:x[k] for k in ('id','attempt_id','state','version','expires_at','updated_at')}|{'expired':x['expires_at']<=now} for x in result]}
