"""Bounded JSONL bridge for registered wrapper commands, not an ACP/A2A claim."""
import json
from .database import RuntimeErrorCode


class JsonlBridge:
    def __init__(self):
        self.seen = b''
        self.offset = 0
        self.session_id = None
        self.result = None
        self.output_count = 0

    def feed(self, data: bytes, *, final=False):
        if not data.startswith(self.seen):
            raise RuntimeErrorCode('OUTPUT_CURSOR_LOST')
        self.seen = data
        while (end := data.find(b'\n', self.offset)) >= 0:
            raw = data[self.offset:end]
            self.offset = end+1
            if not raw.strip(): continue
            if len(raw) > 65536: raise RuntimeErrorCode('RESULT_FORMAT_INVALID')
            try:
                message = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeError):
                raise RuntimeErrorCode('RESULT_FORMAT_INVALID') from None
            if not isinstance(message, dict) or self.result is not None:
                raise RuntimeErrorCode('RESULT_FORMAT_INVALID')
            kind = message.get('type')
            if kind == 'session':
                sid = message.get('session_id')
                if self.session_id or not isinstance(sid, str) or not 1 <= len(sid) <= 160:
                    raise RuntimeErrorCode('SESSION_INVALID')
                self.session_id = sid
            elif kind == 'output' and self.session_id:
                if not isinstance(message.get('text'), str): raise RuntimeErrorCode('RESULT_FORMAT_INVALID')
                if message['text'].strip(): self.output_count += 1
            elif kind == 'result' and self.session_id:
                if message.get('session_id') != self.session_id:
                    raise RuntimeErrorCode('SESSION_CHANGED')
                self.result = message.get('result')
                if not self.valid_result(self.result): raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
            else:
                raise RuntimeErrorCode('RESULT_FORMAT_INVALID')
        if final and (data[self.offset:].strip() or not self.session_id or self.result is None):
            raise RuntimeErrorCode('RESULT_INCOMPLETE')

    @staticmethod
    def valid_result(result):
        return (isinstance(result, dict) and isinstance(result.get('summary'), str)
                and bool(result['summary'].strip()) and isinstance(result.get('evidence'), list)
                and bool(result['evidence']) and all(isinstance(e, str) and e.strip() for e in result['evidence']))

    @classmethod
    def validate(cls, chunks):
        try:
            data = json.loads(b''.join(chunks).decode('utf-8'))
            return cls.valid_result(data)
        except (ValueError, UnicodeError): return False
