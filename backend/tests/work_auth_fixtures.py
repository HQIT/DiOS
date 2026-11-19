import json
from app.runtime.work_policy import FileWorkPolicy


def policy_fixture(root,profile_id,spec,secret_refs=None):
    refs=secret_refs or []
    permissions=[dict(action='runtime.'+action,resource_kind='runtime_profile',resource_id=profile_id,
        resource_version=spec.fingerprint(),constraint_revision='test-policy') for action in ('execute','accept_result')]
    config=dict(version=1,policy_id='test-policy',subject='operator:test',actor='service:test',profiles={profile_id:dict(
        profile_digest=spec.fingerprint(),secret_refs=refs,work_ttl_seconds=86400,
        permissions={layer:permissions for layer in ('caller','service','workload','resource')})})
    path=root/'work-policy.json';path.write_text(json.dumps(config))
    provider=FileWorkPolicy(path)
    return provider,provider.admission(profile_id,spec.fingerprint(),refs)
