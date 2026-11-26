"""Versioned, read-only file references resolved on the trusted Docker host."""
import json
import hashlib
import asyncio
import os
import re
import stat
from pathlib import Path
from .adapter import Mount
from .database import RuntimeErrorCode


def private_file(path,limit):
    """Protect the host path while allowing explicit read-only container access."""
    path=Path(path);descriptor=None
    try:
        if os.name!='posix' or not path.is_absolute() or path.resolve()!=path:
            raise RuntimeErrorCode('SECRET_PATH_INVALID')
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):raise RuntimeErrorCode('SECRET_PATH_INVALID')
        if info.st_uid!=os.getuid() or info.st_mode & 0o7133:
            raise RuntimeErrorCode('SECRET_PERMISSIONS_INVALID')
        parent=path.parent.stat()
        if parent.st_uid!=os.getuid() or parent.st_mode & 0o022:
            raise RuntimeErrorCode('SECRET_DIRECTORY_NOT_PRIVATE')
        if info.st_mode & 0o044:
            if stat.S_IMODE(parent.st_mode)!=0o700:
                raise RuntimeErrorCode('SECRET_DIRECTORY_NOT_PRIVATE')
        with os.fdopen(descriptor,'rb') as stream:
            descriptor=None;raw=stream.read(limit+1)
        if not 0<len(raw)<=limit:raise RuntimeErrorCode('SECRET_SIZE_INVALID')
        return raw
    except OSError:raise RuntimeErrorCode('SECRET_PATH_INVALID') from None
    finally:
        if descriptor is not None:os.close(descriptor)


def validate_worker_mounts(mounts,user):
    parts=user.split(':')
    if len(parts)>2 or not all(p.isdigit() for p in parts) or int(parts[0])==0:
        raise RuntimeErrorCode('SECRET_RUNTIME_USER_DENIED')
    uid=int(parts[0]);gid=int(parts[1]) if len(parts)==2 else None
    for mount in mounts:
        info=Path(mount.source).stat()
        bit=0o400 if info.st_uid==uid else 0o040 if info.st_gid==gid else 0o004
        if not info.st_mode & bit:raise RuntimeErrorCode('SECRET_RUNTIME_USER_DENIED')


async def verify_managed(config,rows):
    from .openbao_secrets import OpenBaoSecrets
    semaphore=asyncio.Semaphore(4)
    async def verify(row):
        async with semaphore:
            raw=await OpenBaoSecrets(config).read_bytes_async(row['secret_ref'])
            if hashlib.sha256(raw).hexdigest()!=row['sha256']:
                raise RuntimeErrorCode('SECRET_CONTENT_CHANGED')
    try:
        async with asyncio.timeout(5),asyncio.TaskGroup() as group:
            for row in rows:group.create_task(verify(row))
    except TimeoutError:raise RuntimeErrorCode('SECRET_PROVIDER_UNAVAILABLE') from None
    except ExceptionGroup as errors:
        # Error details and provider bodies never enter ordinary task records.
        codes={e.code for e in errors.exceptions if isinstance(e,RuntimeErrorCode)}
        code='SECRET_UNAVAILABLE' if 'SOURCE_SECRET_UNAVAILABLE' in codes else (
            'SECRET_CONTENT_CHANGED' if 'SECRET_CONTENT_CHANGED' in codes else 'SECRET_PROVIDER_UNAVAILABLE')
        raise RuntimeErrorCode(code) from None


class FileSecretMounts:
    def __init__(self,path,scope):self.path,self.scope=Path(path),scope

    def resolve(self,profile_id,requests):
        try:config=json.loads(private_file(self.path,65536))
        except RuntimeErrorCode:raise
        except (ValueError,UnicodeError):raise RuntimeErrorCode('SECRET_CATALOG_INVALID') from None
        if not isinstance(config,dict) or config.get('version')!=1:raise RuntimeErrorCode('SECRET_CATALOG_INVALID')
        catalog=config.get('secrets',{})
        if not isinstance(catalog,dict):raise RuntimeErrorCode('SECRET_CATALOG_INVALID')
        mounts=[];managed=[]
        for request in requests:
            row=catalog.get(request['ref'])
            if not isinstance(row,dict) or row.get('state')!='active':raise RuntimeErrorCode('SECRET_UNAVAILABLE')
            profiles=row.get('profiles',[])
            if row.get('scope')!=self.scope or not isinstance(profiles,list) or profile_id not in profiles:
                raise RuntimeErrorCode('SECRET_ACCESS_DENIED')
            if row.get('version')!=request['version']:raise RuntimeErrorCode('SECRET_VERSION_CHANGED')
            path=Path(row.get('path',''))
            expected=row.get('sha256','')
            if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):raise RuntimeErrorCode('SECRET_DIGEST_REQUIRED')
            if hashlib.sha256(private_file(path,16384)).hexdigest()!=expected:raise RuntimeErrorCode('SECRET_CONTENT_CHANGED')
            if 'secret_ref' in row:
                from .openbao_secrets import validate_config,validate_reference
                try:validate_config(config.get('secret_store'));validate_reference(row['secret_ref'])
                except (ValueError,TypeError,KeyError):raise RuntimeErrorCode('SECRET_CATALOG_INVALID') from None
                managed.append(row)
            mounts.append(Mount(str(path.resolve()),request['target'],True))
        if managed:asyncio.run(verify_managed(config['secret_store'],managed))
        return tuple(mounts)
