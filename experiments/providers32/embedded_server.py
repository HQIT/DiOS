"""Isolated experiment gateway for embedded transactional stores; not a production API."""
import argparse,json,os,socketserver,threading,time
from pathlib import Path
import apsw
from .model import initial,raw,transition

class Embedded:
    def __init__(self,kind,path):
        self.kind=kind;self.path=path;self.local=threading.local()
        if kind=='lmdb':
            import lmdb
            self.env=lmdb.open(path,map_size=2*1024*1024*1024,max_dbs=2,sync=True,metasync=True,writemap=False)
            self.states=self.env.open_db(b'states');self.logs=self.env.open_db(b'entries')
        else:
            c=self.connection()
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS states(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,body TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS entry_run ON entries(run_id)')
    def connection(self):
        if not hasattr(self.local,'connection'):
            c=apsw.Connection(self.path+'/store.sqlite')
            c.set_busy_timeout(30000)
            c.execute('PRAGMA synchronous=FULL');c.execute('PRAGMA cache_size=-8192')
            self.local.connection=c
        return self.local.connection
    def command(self,q):
        op=q['op']
        if op=='health':return {'ready':True}
        key=q['scope']+':'+q['run_id'];ek=key+'/'+q.get('request',{}).get('id','')
        started=time.perf_counter()
        if self.kind=='sqlite':
            c=self.connection()
            if op=='state':return json.loads(c.execute('SELECT body FROM states WHERE id=?',(key,)).fetchone()[0])
            if op=='entries':return [json.loads(x[0]) for x in c.execute('SELECT body FROM entries WHERE run_id=?',(key,))]
            c.execute('BEGIN IMMEDIATE');locked=time.perf_counter()
            try:
                if op=='create':c.execute('INSERT OR IGNORE INTO states VALUES(?,?)',(key,raw(initial())));result=None
                else:
                    assert q['run_id']==q['request']['run_id']
                    state=json.loads(c.execute('SELECT body FROM states WHERE id=?',(key,)).fetchone()[0])
                    old=c.execute('SELECT body FROM entries WHERE id=?',(ek,)).fetchone()
                    updated,entry,result=transition(state,q['request'],json.loads(old[0]) if old else None)
                    if updated:
                        c.execute('UPDATE states SET body=? WHERE id=?',(raw(updated),key))
                        c.execute('INSERT INTO entries VALUES(?,?,?)',(ek,key,raw(entry)))
                written=time.perf_counter()
                if q.get('fail_before_commit'):raise OSError('INJECTED_COMMIT_FAILURE')
                c.execute('COMMIT')
            except BaseException:c.execute('ROLLBACK');raise
        else:
            if op in {'state','entries'}:
                with self.env.begin() as tx:
                    if op=='state':return json.loads(tx.get(key.encode(),db=self.states))
                    prefix=(key+'/').encode();out=[]
                    with tx.cursor(db=self.logs) as cur:
                        if cur.set_range(prefix):
                            for k,v in cur:
                                if not k.startswith(prefix):break
                                out.append(json.loads(v))
                    return out
            with self.env.begin(write=True) as tx:
                locked=time.perf_counter()
                if op=='create':tx.put(key.encode(),raw(initial()).encode(),db=self.states,overwrite=False);result=None
                else:
                    assert q['run_id']==q['request']['run_id']
                    state=json.loads(tx.get(key.encode(),db=self.states));old=tx.get(ek.encode(),db=self.logs)
                    updated,entry,result=transition(state,q['request'],json.loads(old) if old else None)
                    if updated:
                        tx.put(key.encode(),raw(updated).encode(),db=self.states)
                        assert tx.put(ek.encode(),raw(entry).encode(),db=self.logs,overwrite=False)
                written=time.perf_counter()
                if q.get('fail_before_commit'):raise OSError('INJECTED_COMMIT_FAILURE')
        ended=time.perf_counter()
        if result is not None:result['timing']={'server_lock_ms':(locked-started)*1000,'transition_write_ms':(written-locked)*1000,'commit_ms':(ended-written)*1000,'retries':0}
        return result

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        import socket
        self.request.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        while line:=self.rfile.readline(1024*1024):
            try:answer={'result':self.server.store.command(json.loads(line))}
            except Exception as e:answer={'error':type(e).__name__,'message':str(e)}
            try:self.wfile.write((raw(answer)+'\n').encode());self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):return
    def finish(self):
        c=getattr(self.server.store.local,'connection',None)
        if c is not None:c.close();del self.server.store.local.connection
        super().finish()

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True;daemon_threads=True;request_queue_size=128

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--kind',choices=['sqlite','lmdb'],required=True);p.add_argument('--path',default='/data');a=p.parse_args()
    Path(a.path).mkdir(exist_ok=True)
    with Server(('0.0.0.0',4321),Handler) as server:
        server.store=Embedded(a.kind,a.path)
        print(json.dumps({'ready':True,'kind':a.kind,'sqlite':apsw.sqlitelibversion()}),flush=True)
        server.serve_forever()
