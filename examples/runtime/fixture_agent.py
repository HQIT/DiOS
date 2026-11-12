"""Synthetic protocol endpoint. Real container/process/session; no model or business code."""
import json
import os
import sys
import time
from uuid import uuid4


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


payload = json.loads(os.environ.get('DIOS_WORK_INPUT', '{}'))
session = str(uuid4())
emit({'type':'session', 'session_id':session})
emit({'type':'output','text':'正在核验合成输入：中文🙂'})
time.sleep(float(payload.get('delay', .5)))
mode = payload.get('mode','success')
if mode == 'crash':
    print('fixture child exited',file=sys.stderr,flush=True)
    sys.exit(7)
if mode == 'incomplete':
    sys.stdout.write('{"type":"result"');sys.stdout.flush();sys.exit(0)
if mode == 'insufficient':
    emit({'type':'result','session_id':session,'result':{'summary':'缺少证据','evidence':[]}})
elif mode == 'overflow':
    sys.stdout.write('x'*2*1024*1024);sys.stdout.flush()
else:
    emit({'type':'result','session_id':session,'result':{
        'summary':'合成任务已完成', 'evidence':['fixture:'+os.environ.get('DIOS_ATTEMPT_ID','standalone')],
        'session_id':session,'pid':os.getpid()}})
