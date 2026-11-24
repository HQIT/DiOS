"""DiOS JSONL v1 and canonical input encoding, independent of backend storage."""
import hashlib
import json
import os
import sys

class WorkloadError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()

def emit(value, *, stream=None):
    raw = encoded(value) + b'\n'
    if len(raw) > 65537:
        raise WorkloadError('OUTPUT_LIMIT_EXCEEDED')
    stream = stream if stream is not None else sys.stdout.buffer
    stream.write(raw)
    stream.flush()

def work_input(environ=None):
    env = os.environ if environ is None else environ
    try:
        raw = env['DIOS_WORK_INPUT']
        if len(raw.encode('utf-8')) > 16384:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        encoded(value)  # reject nonfinite JSON before application code sees it
        return value
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise WorkloadError('INVALID_WORK_INPUT') from None

# Normalized JSONL-v1 terminal error codes accepted by this client version.
TERMINAL_ERROR_CODES = frozenset(['ACP_AUTH_FAILED', 'ACP_FRAME_INVALID', 'ACP_FRAME_TOO_LARGE', 'ACP_PROCESS_EXITED', 'ACP_QUERY_TIMEOUT', 'ACP_REQUEST_FAILED', 'ACP_TOOL_ACTIVITY_CHANGED', 'ACP_TOOL_ACTIVITY_UNVERIFIED', 'ACP_TRANSPORT_FAILED', 'AGENT_EXECUTION_REJECTED', 'EVIDENCE_INSUFFICIENT', 'PROCESS_DEADLINE_EXCEEDED', 'PROCESS_EXITED', 'PROVIDER_NETWORK_ERROR', 'PROVIDER_QUOTA_EXHAUSTED', 'PROVIDER_RATE_LIMITED', 'PROVIDER_TIMEOUT', 'RESULT_EXIT_CONFLICT', 'RESULT_FORMAT_INVALID', 'RESULT_INCOMPLETE', 'REVIEW_BRIDGE_FAILED', 'SESSION_CREATE_FAILED', 'TOOL_APPROVAL_DENIED', 'WORKER_GATEWAY_OBSERVATION_UNAVAILABLE', 'WORK_PREFLIGHT_UNAVAILABLE'])
