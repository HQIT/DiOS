"""Trusted local policy provider. Public request bodies never supply policy layers."""
import hashlib
import json
from pathlib import Path
from .database import RuntimeErrorCode
from .store import encoded, bounded, identifier
from app.services.e2ag import evaluate_work_permission


def permission(record,action):
    return dict(action='runtime.'+action,resource_kind='runtime_profile',resource_id=record['profile_id'],
        resource_version=record['profile_digest'],constraint_revision=record['policy_id'])


class FileWorkPolicy:
    def __init__(self,path):self.path=Path(path)

    def admission(self,profile_id,profile_digest,secret_refs):
        if self.path.stat().st_size>128*1024:raise RuntimeErrorCode('WORK_POLICY_TOO_LARGE')
        config=json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(config,dict) or config.get('version')!=1:raise RuntimeErrorCode('WORK_POLICY_VERSION_UNSUPPORTED')
        if set(config)-{'version','policy_id','subject','actor','profiles'}:raise RuntimeErrorCode('WORK_POLICY_UNSUPPORTED_FIELDS')
        identifier(config.get('policy_id'));identifier(config.get('subject'));identifier(config.get('actor'))
        profiles=config.get('profiles',{})
        if not isinstance(profiles,dict):raise RuntimeErrorCode('WORK_POLICY_INVALID')
        row=profiles.get(profile_id)
        if not isinstance(row,dict):raise RuntimeErrorCode('WORK_PROFILE_NOT_GRANTED')
        if set(row)-{'profile_digest','secret_refs','permissions','work_ttl_seconds'}:raise RuntimeErrorCode('WORK_POLICY_UNSUPPORTED_FIELDS')
        if row.get('profile_digest')!=profile_digest:raise RuntimeErrorCode('WORK_PROFILE_CHANGED')
        if row.get('secret_refs',[])!=secret_refs:raise RuntimeErrorCode('SECRET_REFERENCE_CHANGED')
        ttl=row.get('work_ttl_seconds',86400)
        if type(ttl) is not int or not 60<=ttl<=86400:raise RuntimeErrorCode('WORK_DEADLINE_INVALID')
        result=dict(subject=config['subject'],actor=config['actor'],policy_id=config['policy_id'],
            policy_digest=hashlib.sha256(encoded(config)).hexdigest(),profile_id=profile_id,profile_digest=profile_digest,
            permissions=row.get('permissions',{}),secret_refs=secret_refs,work_ttl_seconds=ttl)
        bounded(result)
        for action in ('execute','accept_result'):
            decision=evaluate_work_permission(result['permissions'],permission(result,action))
            if not decision.permits_execution:raise RuntimeErrorCode(decision.reason_codes[0])
        return result


def security_profile(path):
    raw=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw,dict):raise RuntimeErrorCode('WORK_GOVERNANCE_MODE_INVALID')
    governance=raw.get('governance',{})
    if not isinstance(governance,dict):raise RuntimeErrorCode('WORK_GOVERNANCE_MODE_INVALID')
    mode=governance.get('mode','trusted-local')
    if mode not in {'trusted-local','e2ag'}:raise RuntimeErrorCode('WORK_GOVERNANCE_MODE_INVALID')
    refs=raw.get('secrets',[])
    if not isinstance(refs,list) or len(refs)>16:raise RuntimeErrorCode('SECRET_REFERENCE_INVALID')
    seen=set()
    for row in refs:
        if not isinstance(row,dict) or set(row)!={'ref','version','target'}:raise RuntimeErrorCode('SECRET_REFERENCE_INVALID')
        identifier(row['ref']);identifier(row['version'])
        target=row['target']
        if not isinstance(target,str) or not target.startswith('/run/secrets/') or '..' in Path(target).parts or len(Path(target).parts)!=4:
            raise RuntimeErrorCode('SECRET_TARGET_INVALID')
        if target in seen:raise RuntimeErrorCode('SECRET_TARGET_CONFLICT')
        seen.add(target)
    if refs and mode!='e2ag':raise RuntimeErrorCode('SECRET_AUTHORIZATION_REQUIRED')
    return mode,refs
