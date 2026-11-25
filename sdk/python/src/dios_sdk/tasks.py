"""Synchronous HTTPS task API client. Transport failures never imply task death."""
import hashlib
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from .protocol import WorkloadError, encoded

class ApiError(WorkloadError):
    def __init__(self, code, status):
        self.status = status
        super().__init__(code)

class TransportError(WorkloadError):
    def __init__(self, *, ambiguous=False, idempotency_key=None):
        self.ambiguous = ambiguous
        self.idempotency_key = idempotency_key
        super().__init__('TASK_TRANSPORT_UNAVAILABLE')

class ObservationTimeout(WorkloadError):
    def __init__(self, run_id, last_observation):
        self.run_id, self.last_observation = run_id, last_observation
        super().__init__('TASK_OBSERVATION_TIMEOUT')

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class TaskClient:
    def __init__(self, base_url, token, *, timeout=30, max_bytes=16*1024*1024):
        u = urlsplit(base_url)
        local = u.hostname in {'localhost', '127.0.0.1', '::1'}
        if (u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password
                or u.query or u.fragment or (u.scheme == 'http' and not local)):
            raise ValueError('Use HTTPS, or HTTP on a loopback development endpoint')
        if not isinstance(token, str) or not token or any(c in token for c in '\r\n'):
            raise ValueError('A nonempty access token is required')
        if not 0 < timeout <= 300 or not 1024 <= max_bytes <= 128*1024*1024:
            raise ValueError('Invalid request limits')
        self.base_url = base_url.rstrip('/') + '/api/os/runtime'
        self._token, self.timeout, self.max_bytes = token, timeout, max_bytes
        self._opener = build_opener(ProxyHandler({}) if local else ProxyHandler(), _NoRedirect())

    def _request(self, suffix, *, method='GET', body=None, raw=False, timeout=None):
        headers = {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json'}
        data = encoded(body) if body is not None else None
        if data is not None: headers['Content-Type'] = 'application/json'
        request = Request(self.base_url + suffix, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                value = response.read(self.max_bytes + 1)
                if len(value) > self.max_bytes: raise WorkloadError('TASK_RESPONSE_TOO_LARGE')
        except HTTPError as exc:
            code = 'HTTP_ERROR'
            try:
                detail = json.loads(exc.read(4096)).get('detail')
                if isinstance(detail, str) and re.fullmatch(r'[A-Z0-9_]{1,100}', detail): code = detail
            except (ValueError, TypeError, AttributeError): pass
            finally: exc.close()
            raise ApiError(code, exc.code) from None
        except (URLError, OSError, TimeoutError):
            raise TransportError(ambiguous=method!='GET',
                                 idempotency_key=(body or {}).get('idempotency_key')) from None
        if raw: return value
        try:
            result = json.loads(value.decode('utf-8'))
            if not isinstance(result, dict): raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise WorkloadError('TASK_RESPONSE_INVALID') from None

    def identity(self): return self._request('/identity')
    def issue_delegation(self, parent_ref, profile_id, actor, permissions, expires_at, *, idempotency_key, parent_run_id=None):
        if not isinstance(idempotency_key,str) or not idempotency_key:
            raise ValueError('Supply a stable idempotency key for delegation issuance')
        return self._request('/delegations',method='POST',body=dict(parent_ref=parent_ref,
            profile_id=profile_id,actor=actor,permissions=permissions,expires_at=expires_at,idempotency_key=idempotency_key,
            **({'parent_run_id':parent_run_id} if parent_run_id is not None else {})))
    def delegation(self, reference):
        return self._request('/delegations/'+quote(reference,safe=''))
    def revoke_delegation(self, reference, *, expected_version):
        return self._request('/delegations/'+quote(reference,safe='')+'/revoke',method='POST',body={'expected_version':expected_version})
    def profiles(self): return self._request('/profiles')['items']
    def inputs(self): return self._request('/samples')['items']
    def submit(self, profile_id, input_id, goal, *, idempotency_key, delegation_ref=None, parent_run_id=None):
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError('Supply a stable idempotency key for the logical task')
        return self._request('/runs', method='POST', body=dict(profile_id=profile_id,
            sample_id=input_id, goal=goal, idempotency_key=idempotency_key,
            **({'delegation_ref':delegation_ref} if delegation_ref is not None else {}),
            **({'parent_run_id':parent_run_id} if parent_run_id is not None else {})))
    def submit_input(self, profile_id, input_payload, goal, *, idempotency_key, delegation_ref=None, parent_run_id=None):
        if not isinstance(idempotency_key,str) or not idempotency_key:
            raise ValueError('Supply a stable idempotency key for the logical task')
        return self._request('/runs',method='POST',body=dict(profile_id=profile_id,
            input_payload=input_payload,goal=goal,idempotency_key=idempotency_key,
            **({'delegation_ref':delegation_ref} if delegation_ref is not None else {}),
            **({'parent_run_id':parent_run_id} if parent_run_id is not None else {})))
    def get(self, run_id, *, timeout=None):
        return self._request('/runs/'+quote(run_id,safe=''), timeout=timeout)
    def list(self, *, limit=30, cursor=None):
        return self._request('/runs?'+urlencode(dict(limit=limit, **({'cursor':cursor} if cursor else {}))))
    def cancel(self, run_id, *, expected_version):
        return self._request('/runs/'+quote(run_id,safe='')+'/cancel', method='POST',
                             body={'expected_version':expected_version})
    def wait(self, run_id, *, timeout=300, interval=1):
        if timeout <= 0 or not 0 < interval <= 60: raise ValueError('Invalid observation limits')
        deadline, last = time.monotonic()+timeout, None
        while time.monotonic() < deadline:
            try:
                last = self.get(run_id, timeout=min(self.timeout,max(.001,deadline-time.monotonic())))
                if last.get('state') in {'succeeded','failed','cancelled'}: return last
            except TransportError: pass
            time.sleep(min(interval,max(0,deadline-time.monotonic())))
        raise ObservationTimeout(run_id,last)
    def artifact(self, run_id, artifact_id):
        detail = self.get(run_id)
        record = next((a for a in detail.get('artifacts',[]) if a['id']==artifact_id),None)
        if record is None: raise WorkloadError('ARTIFACT_NOT_FOUND')
        if record['size'] > self.max_bytes: raise WorkloadError('TASK_RESPONSE_TOO_LARGE')
        value = self._request('/runs/'+quote(run_id,safe='')+'/artifacts/'+quote(artifact_id,safe='')+'/download',raw=True)
        if len(value)!=record['size'] or hashlib.sha256(value).hexdigest()!=record['digest']:
            raise WorkloadError('ARTIFACT_DIGEST_MISMATCH')
        return value
