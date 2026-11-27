"""Offline administrator-confirmed activation of a reconciled SQLite installation.

Infrastructure fences the source. This command records the operator's attestation,
checks local evidence, and releases both database gates in one SQLite transaction.
"""
import hashlib
import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
import sqlite3
import time

from app.runtime.database import RuntimeErrorCode
from app.runtime.recovery import _database_info, _file_info, _sync_dir
from app.runtime.storage_lease import StorageLease, database_lock, artifacts_lock
from app.runtime.store import encoded
from .installation_readiness import inspect_restore

SECTIONS=('source_isolation','checkpoint_reconciliation','remote_reauthentication','resource_review')


def read_document(path, *, limit=65536):
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:
        raise RuntimeErrorCode('INVALID_ACTIVATION_DOCUMENT')
    return json.loads(path.read_bytes())


def publish_document(path, value):
    # Reuse LocalArtifacts' atomic, no-overwrite publication protocol.
    fd, temporary = tempfile.mkstemp(prefix='.activation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        _sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def activation_evidence(path, receipt_digest):
    value=read_document(Path(path))
    if not isinstance(value,dict) or set(value)!={'format','restore_receipt_sha256','operator',*SECTIONS}:
        raise RuntimeErrorCode('INVALID_ACTIVATION_EVIDENCE')
    if value['format']!=1 or value['restore_receipt_sha256']!=receipt_digest:
        raise RuntimeErrorCode('ACTIVATION_RESTORE_MISMATCH')
    if not isinstance(value['operator'],str) or not 1<=len(value['operator'])<=160:
        raise RuntimeErrorCode('INVALID_ACTIVATION_EVIDENCE')
    for name in SECTIONS:
        row=value[name]
        if not isinstance(row,dict) or set(row)!={'method','evidence_ref'} or row['method']!='administrator-confirmed':
            raise RuntimeErrorCode('INVALID_ACTIVATION_EVIDENCE')
        if not isinstance(row['evidence_ref'],str) or not 1<=len(row['evidence_ref'])<=1024:
            raise RuntimeErrorCode('INVALID_ACTIVATION_EVIDENCE')
    return value


def modes(db):
    runtime=db.execute("SELECT mode,revision FROM main.runtime_lifecycle WHERE key='global'").fetchone()
    service=db.execute("SELECT scope,mode,revision FROM service.managed_runtime_control WHERE scope='__dios_installation_restore__'").fetchone()
    return {'runtime':list(runtime) if runtime else None,'service':list(service) if service else None}


def activate_installation(destination, evidence_file, *, confirm_source_isolated=False):
    if confirm_source_isolated is not True:
        raise RuntimeErrorCode('SOURCE_ISOLATION_CONFIRMATION_REQUIRED')
    root=Path(destination).resolve()
    receipt_path=root/'installation-restore.json'
    receipt=read_document(receipt_path,limit=16*1024*1024)
    if receipt.get('format')!=1 or receipt.get('mode')!='restore_hold':
        raise RuntimeErrorCode('INVALID_INSTALLATION_RESTORE_RECEIPT')
    receipt_digest=_file_info(receipt_path)['sha256']
    evidence=activation_evidence(evidence_file,receipt_digest)
    evidence_digest=hashlib.sha256(encoded(evidence)).hexdigest()
    runtime=root/'runtime/state.sqlite';service=root/'service.sqlite';objects=root/'runtime/objects'
    intent_path=root/'installation-activation-intent.json'
    result_path=root/'installation-activation.json'
    for path in (runtime,service,objects):
        if path.is_symlink() or not path.exists():raise RuntimeErrorCode('INVALID_ACTIVATION_PATH')
    with ExitStack() as stack:
        for lock in (database_lock(runtime),database_lock(service),artifacts_lock(objects)):
            stack.enter_context(StorageLease(lock,exclusive=True))
        if result_path.exists():
            result=read_document(result_path)
            if result.get('evidence_sha256')!=evidence_digest or result.get('restore_receipt_sha256')!=receipt_digest:
                raise RuntimeErrorCode('ACTIVATION_EVIDENCE_CONFLICT')
            return result
        db=sqlite3.connect(runtime,timeout=1,isolation_level=None)
        stack.callback(db.close)
        db.execute('ATTACH DATABASE ? AS service',(str(service),))
        # SQLite super-journal gives cross-file atomicity only outside WAL mode.
        for name in ('main','service'):
            if db.execute(f'PRAGMA {name}.journal_mode=DELETE').fetchone()[0]!='delete':
                raise RuntimeErrorCode('ACTIVATION_ATOMIC_JOURNAL_REQUIRED')
            db.execute(f'PRAGMA {name}.synchronous=FULL')
        for spool in sorted((root/'spools').glob('*.sqlite')):
            if spool.is_symlink():raise RuntimeErrorCode('INVALID_ACTIVATION_PATH')
            connection=sqlite3.connect(spool,timeout=1,isolation_level=None)
            stack.callback(connection.close)
            connection.execute('BEGIN IMMEDIATE')
        db.execute('BEGIN IMMEDIATE')
        try:
            before=modes(db)
            intent=read_document(intent_path) if intent_path.exists() else None
            if intent:
                if intent.get('evidence_sha256')!=evidence_digest or intent.get('restore_receipt_sha256')!=receipt_digest or intent.get('destination')!=str(root):
                    raise RuntimeErrorCode('ACTIVATION_EVIDENCE_CONFLICT')
                if before==intent['after']:
                    # The transaction committed but final receipt publication was interrupted.
                    db.commit()
                    result=dict(intent,status='activated')
                    publish_document(result_path,result)
                    _sync_dir(root)
                    return result
                if before!=intent['before']:raise RuntimeErrorCode('ACTIVATION_STATE_CHANGED')
            if db.execute("SELECT 1 FROM service.managed_runtime_control WHERE mode='restore_hold' AND scope!='__dios_installation_restore__' LIMIT 1").fetchone():
                raise RuntimeErrorCode('RESTORE_HOLD_REQUIRED')
            if db.execute('PRAGMA service.integrity_check').fetchall()!=[('ok',)] or db.execute('PRAGMA service.foreign_key_check').fetchone():
                raise RuntimeErrorCode('BACKUP_DATABASE_INVALID')
            observation=inspect_restore(root)
            if not observation['local_execution_settled']:
                raise RuntimeErrorCode('RESTORE_RECONCILIATION_REQUIRED')
            if before['runtime'] is None or before['runtime'][0]!='restore_hold' or before['service'] is None or before['service'][1]!='restore_hold':
                raise RuntimeErrorCode('RESTORE_HOLD_REQUIRED')
            inventory=_database_info(runtime)
            for key,expected in inventory['objects'].items():
                path=objects/key
                if path.is_symlink() or not path.is_file() or _file_info(path)!=expected:
                    raise RuntimeErrorCode('BACKUP_ARTIFACT_MISMATCH')
            if intent is None:
                after={'runtime':['running',before['runtime'][1]+1],
                    'service':[before['service'][0],'running',before['service'][2]+1]}
                intent={'format':1,'destination':str(root),'restore_receipt_sha256':receipt_digest,
                    'evidence_sha256':evidence_digest,'operator':evidence['operator'],
                    'external_confirmations':{name:evidence[name] for name in SECTIONS},
                    'before':before,'after':after,'prepared_at':time.time(),
                    'runtime_counts':inventory['counts'],'verified_objects':len(inventory['objects'])}
                publish_document(intent_path,intent)
                _sync_dir(root)
            db.execute("UPDATE main.runtime_lifecycle SET mode='running',revision=revision+1,updated_at=? WHERE key='global'",(time.time(),))
            db.execute("UPDATE service.managed_runtime_control SET mode='running',revision=revision+1,updated_at=datetime('now') WHERE scope='__dios_installation_restore__'")
            if modes(db)!=intent['after']:raise RuntimeErrorCode('ACTIVATION_STATE_CHANGED')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        result=dict(intent,status='activated')
        publish_document(result_path,result)
        _sync_dir(root)
        return result
