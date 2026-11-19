"""Versioned, read-only file references resolved on the trusted Docker host."""
import json
import hashlib
import re
from pathlib import Path
from .adapter import Mount
from .database import RuntimeErrorCode


class FileSecretMounts:
    def __init__(self,path,scope):self.path,self.scope=Path(path),scope

    def resolve(self,profile_id,requests):
        if self.path.stat().st_size>65536:raise RuntimeErrorCode('SECRET_CATALOG_INVALID')
        config=json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(config,dict) or config.get('version')!=1:raise RuntimeErrorCode('SECRET_CATALOG_INVALID')
        catalog=config.get('secrets',{})
        if not isinstance(catalog,dict):raise RuntimeErrorCode('SECRET_CATALOG_INVALID')
        mounts=[]
        for request in requests:
            row=catalog.get(request['ref'])
            if not isinstance(row,dict) or row.get('state')!='active':raise RuntimeErrorCode('SECRET_UNAVAILABLE')
            profiles=row.get('profiles',[])
            if row.get('scope')!=self.scope or not isinstance(profiles,list) or profile_id not in profiles:
                raise RuntimeErrorCode('SECRET_ACCESS_DENIED')
            if row.get('version')!=request['version']:raise RuntimeErrorCode('SECRET_VERSION_CHANGED')
            path=Path(row.get('path',''))
            if not path.is_absolute() or path.is_symlink() or not path.is_file():raise RuntimeErrorCode('SECRET_PATH_INVALID')
            if not 0<path.stat().st_size<=16384:raise RuntimeErrorCode('SECRET_SIZE_INVALID')
            expected=row.get('sha256','')
            if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):raise RuntimeErrorCode('SECRET_DIGEST_REQUIRED')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise RuntimeErrorCode('SECRET_CONTENT_CHANGED')
            mounts.append(Mount(str(path.resolve()),request['target'],True))
        return tuple(mounts)
