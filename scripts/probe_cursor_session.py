import hashlib, json, os, queue, subprocess, tempfile, threading, time, sys
from pathlib import Path

OUT=Path('work/agent-capability/cursor-session-20261111.json')
MODEL_PROBE='--model-probe' in sys.argv
if MODEL_PROBE:OUT=OUT.with_name('cursor-model-session-20261111.json')
BASE=Path(os.environ['LOCALAPPDATA'])/'cursor-agent/versions/2026.11.05-86047df'
class Peer:
    def __init__(self,cwd):
        self.p=subprocess.Popen([str(BASE/'node.exe'),str(BASE/'index.js'),'acp'],cwd=cwd,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.q=queue.Queue();self.counter=0;self.events=[];self.denials=0;self.output=[]
        threading.Thread(target=self.read,daemon=True).start()
        threading.Thread(target=lambda:self.p.stderr.read(),daemon=True).start()
    def read(self):
        for line in self.p.stdout:
            try:self.q.put(json.loads(line))
            except (ValueError,UnicodeError):pass
    def send(self,obj):
        self.p.stdin.write((json.dumps(obj)+'\n').encode());self.p.stdin.flush()
    def call(self,method,params,timeout=25):
        self.counter+=1;request_id=self.counter
        self.send({'jsonrpc':'2.0','id':request_id,'method':method,'params':params})
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            try:msg=self.q.get(timeout=max(.01,until-time.monotonic()))
            except queue.Empty:break
            if msg.get('id')==request_id and 'method' not in msg:return msg
            if 'method' in msg:
                self.events.append(msg['method'])
                update=msg.get('params',{}).get('update',{})
                if update.get('sessionUpdate')=='agent_message_chunk':
                    content=update.get('content',{})
                    if content.get('type')=='text':self.output.append(content.get('text',''))
                if 'id' in msg:
                    if msg['method']=='session/request_permission':
                        opts=msg.get('params',{}).get('options',[])
                        denied=next((o for o in opts if o.get('kind')=='reject_once'),None)
                        outcome={'outcome':'selected','optionId':denied['optionId']} if denied else {'outcome':'cancelled'}
                        self.send({'jsonrpc':'2.0','id':msg['id'],'result':{'outcome':outcome}});self.denials+=1
                    else:self.send({'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32601,'message':'Survey client does not enable this operation'}})
        return {'error':{'code':'LOCAL_TIMEOUT','message':'bounded observation timeout'}}
    def close(self):
        self.p.stdin.close()
        try:self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill','/PID',str(self.p.pid),'/T','/F'],stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            self.p.wait(timeout=5)
        return self.p.returncode

result={'date':'2026-11-11','version':BASE.name,'model_prompt_sent':False,'probes':[]}
def record(name,response):
    # Authentication errors are summarized; no credential, endpoint, or account detail is saved.
    if 'error' in response:
        clean={'error_code':response['error'].get('code'),'message':str(response['error'].get('message',''))[:160]}
        if name.startswith('session/load'):clean['validation_detail']=str(response['error'].get('data',''))[:1000]
    else:
        raw=response.get('result',{})
        clean={k:v for k,v in raw.items() if k in ['protocolVersion','agentCapabilities','modes','stopReason']}
        if 'sessionId' in raw:clean['session_id_sha256']=hashlib.sha256(raw['sessionId'].encode()).hexdigest()
        if not clean:clean={'response_received':True}
    result['probes'].append({'name':name,**clean});print(json.dumps({'probe':name,**clean}),flush=True)

with tempfile.TemporaryDirectory(prefix='dios-f57-session-') as cwd:
    peer=Peer(cwd)
    try:
        record('initialize',peer.call('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'dios-f57-survey','version':'0.1'}}))
        auth=peer.call('authenticate',{'methodId':'cursor_login'});record('authenticate',auth)
        if 'error' not in auth:
            created=peer.call('session/new',{'cwd':cwd,'mcpServers':[]});record('session/new',created)
            sid=created.get('result',{}).get('sessionId')
            if sid:
                record('session/set_mode ask',peer.call('session/set_mode',{'sessionId':sid,'modeId':'ask'}))
                if MODEL_PROBE:
                    result['model_prompt_sent']=True
                    response=peer.call('session/prompt',{'sessionId':sid,'prompt':[{'type':'text','text':'This is an isolated synthetic protocol test. Do not use tools, files, network or commands. Statically review this Python function: def first(xs): return xs[0]. Return only JSON with keys issue, condition, fixed_code. Keep under 100 words.'}]},timeout=60)
                    record('session/prompt synthetic static review',response)
                    result['assistant_output']=''.join(peer.output)[:3000]
                peer.send({'jsonrpc':'2.0','method':'session/cancel','params':{'sessionId':sid}})
                result['idle_cancel_notification_sent']=True
                record('session/load same-process',peer.call('session/load',{'sessionId':sid,'cwd':cwd,'mcpServers':[]}))
    finally:
        result['first_process_exit']=peer.close();result['events']=peer.events;result['permission_requests']=peer.denials
    if 'sid' in locals() and sid:
        peer=Peer(cwd)
        try:
            peer.call('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'dios-f57-survey','version':'0.1'}})
            auth=peer.call('authenticate',{'methodId':'cursor_login'});record('authenticate after-process-restart',auth)
            if 'error' not in auth:record('session/load after-process-restart',peer.call('session/load',{'sessionId':sid,'cwd':cwd,'mcpServers':[]}))
        finally:result['second_process_exit']=peer.close()
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('Survey saved; model_prompt_sent='+str(result['model_prompt_sent']),flush=True)
