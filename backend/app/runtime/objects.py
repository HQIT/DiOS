"""Runtime-neutral object references over immutable files and existing native IDs.

Trusted repository only. HTTP consumers must authorize the original Run or
operator before resolving metadata. A content digest never grants access.
"""
import asyncio
import re
from uuid import uuid4

from sqlalchemy import select

from .database import RuntimeErrorCode
from .object_schema import contents, heads, links, receipts, require
from .schema import artifacts, attempts, runs
from .store import bounded, digest, identifier

KINDS = {'file','session','artifact'}


def reference(kind, object_id, version=1):
    if kind not in KINDS or type(version) is not int or version < 1:
        raise RuntimeErrorCode('INVALID_OBJECT_REFERENCE')
    identifier(object_id)
    return {'kind':kind,'id':object_id,'version':version}


def validate_ref(value):
    if not isinstance(value,dict) or set(value)!={'kind','id','version'}:
        raise RuntimeErrorCode('INVALID_OBJECT_REFERENCE')
    return reference(value['kind'],value['id'],value['version'])


class Objects:
    def __init__(self, db, scope):
        identifier(scope)
        self.db,self.scope=db,scope

    async def _get(self,c,ref):
        ref=validate_ref(ref)
        kind,oid,version=ref['kind'],ref['id'],ref['version']
        if kind=='file':
            row=(await c.execute(select(contents).where(contents.c.scope==self.scope,
                contents.c.object_id==oid,contents.c.version==version))).mappings().first()
            if row is None:raise RuntimeErrorCode('NOT_FOUND')
            return {'ref':ref,'descriptor':{'digest':'sha256:'+row['digest'],'size':row['size'],'media_type':row['media_type']},
                'created_at':row['created_at'],'created_by':row['created_by'],
                'binding':None,'storage':{'provider':row['provider'],'key':row['storage_key']}}
        if version!=1:raise RuntimeErrorCode('NOT_FOUND')
        table=artifacts if kind=='artifact' else attempts
        row=(await c.execute(select(table).join(runs,table.c.run_id==runs.c.id).where(
            table.c.id==oid,runs.c.scope==self.scope))).mappings().first()
        if row is None or kind=='session' and not row['session_id']:
            raise RuntimeErrorCode('NOT_FOUND')
        if kind=='artifact':
            return {'ref':ref,'descriptor':{'digest':'sha256:'+row['digest'],'size':row['size'],'media_type':'application/octet-stream'},
                'created_at':row['created_at'],'binding':{'run_id':row['run_id'],'attempt_id':row['attempt_id'],'artifact_id':row['id']},
                'storage':{'provider':row['provider'],'key':row['storage_key']}}
        return {'ref':ref,'descriptor':None,'created_at':row['started_at'],
            'binding':{'run_id':row['run_id'],'attempt_id':row['id'],'session_id':row['session_id']},
            'storage':None}

    async def get(self,ref):
        async with self.db.read(self.scope) as c:
            await require(c)
            return await self._get(c,ref)

    async def publish(self,provider,chunks,key,actor,*,object_id=None,expected_version=0,media_type='application/octet-stream'):
        identifier(key);identifier(actor)
        if type(expected_version) is not int or expected_version<0:
            raise RuntimeErrorCode('INVALID_EXPECTED_VERSION')
        if object_id is not None:identifier(object_id)
        if (object_id is None)!=(expected_version==0):raise RuntimeErrorCode('INVALID_OBJECT_VERSION')
        if not isinstance(media_type,str) or len(media_type)>160 or not re.fullmatch(r'[\w.+-]+/[\w.+-]+',media_type):
            raise RuntimeErrorCode('INVALID_MEDIA_TYPE')
        blob=await asyncio.to_thread(provider.put,chunks)
        body={'id':object_id,'expected_version':expected_version,'digest':blob.digest,'size':blob.size,
            'provider':provider.name,'key':blob.key,'media_type':media_type,'actor':actor}
        request_digest=digest(body)
        async with self.db.transaction(self.scope) as c:
            await require(c)
            old=(await c.execute(select(receipts).where(receipts.c.scope==self.scope,receipts.c.key==key))).mappings().first()
            if old:
                if old['request_digest']!=request_digest:raise RuntimeErrorCode('IDEMPOTENCY_CONFLICT')
                return await self._get(c,reference('file',old['object_id'],old['version']))
            now=await self.db.now(c)
            if object_id is None:
                object_id=str(uuid4())
                await c.execute(heads.insert().values(scope=self.scope,id=object_id,version=1,created_at=now))
            else:
                changed=await c.execute(heads.update().where(heads.c.scope==self.scope,heads.c.id==object_id,
                    heads.c.version==expected_version).values(version=expected_version+1))
                if changed.rowcount!=1:raise RuntimeErrorCode('VERSION_CONFLICT')
            version=expected_version+1
            await c.execute(contents.insert().values(scope=self.scope,object_id=object_id,version=version,
                digest=blob.digest,size=blob.size,media_type=media_type,provider=provider.name,storage_key=blob.key,created_by=actor,created_at=now))
            await c.execute(receipts.insert().values(scope=self.scope,key=key,request_digest=request_digest,object_id=object_id,version=version))
            return await self._get(c,reference('file',object_id,version))

    async def versions(self,kind,object_id,*,after=0,limit=50):
        reference(kind,object_id)
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:
            raise RuntimeErrorCode('INVALID_PAGE')
        async with self.db.read(self.scope) as c:
            await require(c)
            if kind!='file':
                item=await self._get(c,reference(kind,object_id))
                return {'items':[] if after>=1 else [item],'next_after':None}
            exists=(await c.execute(select(heads.c.id).where(heads.c.scope==self.scope,heads.c.id==object_id))).first()
            if not exists:raise RuntimeErrorCode('NOT_FOUND')
            rows=(await c.execute(select(contents.c.version).where(contents.c.scope==self.scope,contents.c.object_id==object_id,
                contents.c.version>after).order_by(contents.c.version).limit(limit+1))).scalars().all()
            return {'items':[await self._get(c,reference(kind,object_id,v)) for v in rows[:limit]],
                'next_after':rows[limit-1] if len(rows)>limit else None}

    async def link(self,source,relation,target,actor):
        validate_ref(source);identifier(actor)
        if relation not in {'derived_from','part_of','associated_with'}:raise RuntimeErrorCode('INVALID_OBJECT_RELATION')
        if not isinstance(target,dict):raise RuntimeErrorCode('INVALID_OBJECT_TARGET')
        if set(target)=={'namespace','key','version'}:
            for value in target.values():identifier(value)
            external=True
        else:
            validate_ref(target);external=False
            if source==target:raise RuntimeErrorCode('INVALID_OBJECT_RELATION')
        bounded(target,1024)
        link_id=digest([self.scope,source,relation,target])
        async with self.db.transaction(self.scope) as c:
            await require(c);await self._get(c,source)
            if not external:await self._get(c,target)
            old=(await c.execute(select(links).where(links.c.scope==self.scope,links.c.id==link_id))).mappings().first()
            if old:return dict(old)
            if relation=='derived_from' and not external:
                from .object_lifetime import ObjectLifetime,ref_key
                lifetime=ObjectLifetime(self.db,self.scope)
                await lifetime.available(c,source)
                effective=await lifetime.effective(c,target,forbidden=ref_key(source))
                if effective['state']!='active':raise RuntimeErrorCode('NOT_FOUND')
            row={'scope':self.scope,'id':link_id,'source_kind':source['kind'],'source_id':source['id'],'source_version':source['version'],
                'relation':relation,'target':target,'created_by':actor,'created_at':await self.db.now(c)}
            await c.execute(links.insert().values(**row))
            return row

    async def relations(self,ref,*,after='',limit=50):
        validate_ref(ref)
        if not isinstance(after,str) or len(after)>64 or type(limit) is not int or not 1<=limit<=100:
            raise RuntimeErrorCode('INVALID_PAGE')
        async with self.db.read(self.scope) as c:
            await require(c);await self._get(c,ref)
            rows=[dict(r) for r in (await c.execute(select(links).where(links.c.scope==self.scope,
                links.c.source_kind==ref['kind'],links.c.source_id==ref['id'],links.c.source_version==ref['version'],links.c.id>after)
                .order_by(links.c.id).limit(limit+1))).mappings()]
            return {'items':rows[:limit],'next_after':rows[limit-1]['id'] if len(rows)>limit else None}


def public(value):
    return {k:v for k,v in value.items() if k!='storage'}
