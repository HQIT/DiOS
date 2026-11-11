"""SQLite recovery bundles: consistent database snapshot plus referenced local blobs."""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from sqlalchemy.engine import make_url

from .database import Database, RuntimeErrorCode, SCHEMA_CHECKSUM
from .schema import metadata as base_metadata
from .history_schema import metadata as history_metadata, CHECKSUM as HISTORY_CHECKSUM
from .lifecycle_schema import metadata as lifecycle_metadata, CHECKSUM as LIFECYCLE_CHECKSUM
from .lifecycle import Lifecycle
from .artifacts import LocalArtifacts


def sqlite_path(url):
    parsed=make_url(url)
    if parsed.get_backend_name()!='sqlite': raise RuntimeErrorCode('BACKUP_PROVIDER_UNSUPPORTED')
    if not parsed.database or parsed.database==':memory:' or parsed.query:
        raise RuntimeErrorCode('BACKUP_FILE_REQUIRED')
    path=Path(parsed.database).resolve()
    if not path.is_file(): raise RuntimeErrorCode('BACKUP_SOURCE_NOT_FOUND')
    return path


def _sync_dir(path):
    if os.name!='nt':
        fd=os.open(path,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)


def _write(path, chunks):
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    h=hashlib.sha256();size=0
    with os.fdopen(fd,'wb') as out:
        for chunk in chunks:
            out.write(chunk);h.update(chunk);size+=len(chunk)
        out.flush();os.fsync(out.fileno())
    return {'sha256':h.hexdigest(),'size':size}


def _chunks(path):
    if path.is_symlink(): raise RuntimeErrorCode('BACKUP_FILE_INVALID')
    with path.open('rb') as source:
        while chunk:=source.read(65536):yield chunk


def _file_info(path):
    h=hashlib.sha256();size=0
    for chunk in _chunks(path):h.update(chunk);size+=len(chunk)
    return {'sha256':h.hexdigest(),'size':size}


def _database_info(path):
    c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        if c.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
            raise RuntimeErrorCode('BACKUP_DATABASE_INVALID')
        if c.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeErrorCode('BACKUP_DATABASE_INVALID')
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        components=[('runtime_schema',base_metadata,SCHEMA_CHECKSUM),
            ('runtime_history_schema',history_metadata,HISTORY_CHECKSUM),
            ('runtime_lifecycle_schema',lifecycle_metadata,LIFECYCLE_CHECKSUM)]
        known=set().union(*(set(m.tables) for _,m,_ in components))
        if any(t.startswith('runtime_') and t not in known for t in tables):
            raise RuntimeErrorCode('BACKUP_SCHEMA_INCOMPATIBLE')
        schemas={}
        for version_table,meta,checksum in components:
            present=tables.intersection(meta.tables)
            if not present and version_table!='runtime_schema':continue
            if not set(meta.tables).issubset(tables): raise RuntimeErrorCode('BACKUP_SCHEMA_INCOMPATIBLE')
            rows=c.execute('SELECT version,checksum FROM '+version_table).fetchall()
            if rows!=[(1,checksum)]: raise RuntimeErrorCode('BACKUP_SCHEMA_INCOMPATIBLE')
            for name,table in meta.tables.items():
                actual={r[1] for r in c.execute('PRAGMA table_info('+name+')')}
                if not set(table.columns.keys()).issubset(actual): raise RuntimeErrorCode('BACKUP_SCHEMA_INCOMPATIBLE')
            schemas[version_table]={'version':1,'checksum':checksum}
        objects={}
        for provider,key,hashed,size in c.execute('SELECT provider,storage_key,digest,size FROM runtime_artifacts'):
            if provider!=LocalArtifacts.name: raise RuntimeErrorCode('BACKUP_ARTIFACT_PROVIDER_UNSUPPORTED')
            if not re.fullmatch('[0-9a-f]{64}',key) or key!=hashed or size<0:
                raise RuntimeErrorCode('BACKUP_ARTIFACT_INVALID')
            entry={'sha256':hashed,'size':size}
            if key in objects and objects[key]!=entry: raise RuntimeErrorCode('BACKUP_ARTIFACT_INVALID')
            objects[key]=entry
        counts={name:c.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
            for name in ('runtime_runs','runtime_attempts','runtime_events','runtime_artifacts','runtime_imports')}
        return {'schemas':schemas,'objects':objects,'counts':counts}
    finally:c.close()


def backup_sqlite(url, artifact_root, destination, *, timeout=60):
    """Online SQLite snapshot. Call from a worker thread, never an async event loop."""
    started=time.monotonic()
    if not math.isfinite(timeout) or timeout<=0: raise RuntimeErrorCode('INVALID_BACKUP_LIMIT')
    source=sqlite_path(url)
    artifact_root=Path(artifact_root).resolve()
    if not artifact_root.is_dir(): raise RuntimeErrorCode('BACKUP_ARTIFACT_ROOT_REQUIRED')
    destination=Path(destination).resolve()
    # A reserved new directory prevents overwrite; no manifest means incomplete.
    destination.mkdir(mode=0o700)
    objects_dir=destination/'objects';objects_dir.mkdir(mode=0o700)
    snapshot=destination/'state.sqlite'
    fd=os.open(snapshot,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    incoming=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=1)
    outgoing=sqlite3.connect(snapshot)
    def budget(*_):
        if time.monotonic()-started>timeout: raise RuntimeErrorCode('BACKUP_TIMEOUT')
    try:
        incoming.backup(outgoing,pages=256,progress=budget,sleep=.01)
        outgoing.execute('PRAGMA journal_mode=DELETE')
    finally:outgoing.close();incoming.close()
    info=_database_info(snapshot)
    for key,expected in info['objects'].items():
        budget()
        def chunks():
            for chunk in _chunks(artifact_root/key):budget();yield chunk
        if _write(objects_dir/key,chunks())!=expected:
            raise RuntimeErrorCode('BACKUP_ARTIFACT_MISMATCH')
    # Flush database pages before publishing the complete manifest.
    with snapshot.open('rb') as f:os.fsync(f.fileno())
    _sync_dir(objects_dir)
    manifest={'format':1,'provider':'sqlite-local-v1','created_at':time.time(),
        'database':_file_info(snapshot),**info,'duration_seconds':round(time.monotonic()-started,3)}
    encoded_manifest=json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode('utf-8')
    if len(encoded_manifest)>8*1024*1024:raise RuntimeErrorCode('BACKUP_MANIFEST_TOO_LARGE')
    _write(destination/'manifest.json',[encoded_manifest])
    _sync_dir(destination);_sync_dir(destination.parent)
    return manifest


def verify_backup(bundle):
    bundle=Path(bundle).resolve()
    path=bundle/'manifest.json'
    if (bundle/'objects').is_symlink():raise RuntimeErrorCode('BACKUP_FILE_INVALID')
    if not path.is_file() or path.is_symlink() or path.stat().st_size>8*1024*1024:
        raise RuntimeErrorCode('BACKUP_MANIFEST_INVALID')
    try:
        manifest=json.loads(path.read_text(encoding='utf-8'))
        if manifest['format']!=1 or manifest['provider']!='sqlite-local-v1':raise ValueError()
        if not isinstance(manifest['objects'],dict):raise ValueError()
        for key,value in manifest['objects'].items():
            if not re.fullmatch('[0-9a-f]{64}',key) or value['sha256']!=key or type(value['size']) is not int or value['size']<0:
                raise ValueError()
        if _file_info(bundle/'state.sqlite')!=manifest['database']:
            raise RuntimeErrorCode('BACKUP_DATABASE_MISMATCH')
        actual=_database_info(bundle/'state.sqlite')
        if any(actual[k]!=manifest[k] for k in ('schemas','objects','counts')):
            raise RuntimeErrorCode('BACKUP_MANIFEST_MISMATCH')
        for key,value in manifest['objects'].items():
            if _file_info(bundle/'objects'/key)!=value:raise RuntimeErrorCode('BACKUP_ARTIFACT_MISMATCH')
        return manifest
    except (KeyError,TypeError,ValueError,UnicodeError) as exc:
        if isinstance(exc,RuntimeErrorCode):raise
        raise RuntimeErrorCode('BACKUP_MANIFEST_INVALID') from None
    except FileNotFoundError:
        raise RuntimeErrorCode('BACKUP_FILE_MISSING') from None


def _prepare_restore(bundle,destination):
    manifest=verify_backup(bundle)
    destination.mkdir(mode=0o700)
    (destination/'objects').mkdir(mode=0o700)
    if _write(destination/'restore.pending.sqlite',_chunks(bundle/'state.sqlite'))!=manifest['database']:
        raise RuntimeErrorCode('BACKUP_DATABASE_MISMATCH')
    for key,expected in manifest['objects'].items():
        if _write(destination/'objects'/key,_chunks(bundle/'objects'/key))!=expected:
            raise RuntimeErrorCode('BACKUP_ARTIFACT_MISMATCH')
    return manifest


async def restore_sqlite(bundle,destination):
    """Restore to a fresh private directory and hold all scheduling before publication."""
    started=time.monotonic()
    bundle,destination=Path(bundle).resolve(),Path(destination).resolve()
    manifest=await asyncio.to_thread(_prepare_restore,bundle,destination)
    pending=destination/'restore.pending.sqlite'
    db=Database('sqlite+aiosqlite:///'+str(pending))
    try:
        await db.migrate()
        await Lifecycle(db,'local').hold_restored()
    finally:await db.close()
    def publish():
        with pending.open('rb') as f:os.fsync(f.fileno())
        _sync_dir(destination/'objects')
        os.link(pending,destination/'state.sqlite')  # exclusive publication, no replacement
        pending.unlink()
        receipt={'format':1,'mode':'restore_hold','source_database':manifest['database'],
            'restored_database':_file_info(destination/'state.sqlite'),
            'counts':manifest['counts'],'duration_seconds':round(time.monotonic()-started,3),
            'requires':['source-controller-isolation','remote-reference-reauthentication','task-and-effect-reconciliation']}
        _write(destination/'restore.json',[json.dumps(receipt,sort_keys=True).encode()])
        _sync_dir(destination);_sync_dir(destination.parent)
        return receipt
    return await asyncio.to_thread(publish)
