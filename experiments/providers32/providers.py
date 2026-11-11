import asyncio,json,os,time
import asyncpg,aiomysql
from .model import initial,raw,transition

class SQLStore:
    def __init__(self,kind,scope,*,serial=False):
        self.kind,self.scope,self.serial=kind,scope,serial
    async def start(self):
        if self.kind=='pg':
            self.pool=await asyncpg.create_pool(os.environ['P32_PG_URL'],min_size=32,max_size=32,command_timeout=30)
            async with self.pool.acquire() as c:
                await c.execute('CREATE TABLE IF NOT EXISTS p32_states (id VARCHAR(180) PRIMARY KEY,body TEXT NOT NULL)')
                await c.execute('CREATE TABLE IF NOT EXISTS p32_entries (id VARCHAR(180) PRIMARY KEY,run_id VARCHAR(180) NOT NULL,body TEXT NOT NULL)')
                await c.execute('CREATE INDEX IF NOT EXISTS p32_entries_run ON p32_entries(run_id)')
        else:
            self.pool=await aiomysql.create_pool(host='127.0.0.1',port=15441,user='root',password=os.environ['P32_MYSQL_PASSWORD'],db='p32',minsize=32,maxsize=32,autocommit=False,charset='utf8mb4',init_command='SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
            async with self.pool.acquire() as c:
                async with c.cursor() as q:
                    await q.execute('CREATE TABLE IF NOT EXISTS p32_states (id VARCHAR(180) PRIMARY KEY,body MEDIUMTEXT NOT NULL) ENGINE=InnoDB')
                    await q.execute('CREATE TABLE IF NOT EXISTS p32_entries (id VARCHAR(180) PRIMARY KEY,run_id VARCHAR(180) NOT NULL,body MEDIUMTEXT NOT NULL,INDEX(run_id)) ENGINE=InnoDB')
                await c.commit()
        if self.serial:await self.create('__workspace_lock__')
        return self
    def key(self,key):return self.scope+':'+key
    async def create(self,run_id):
        if self.kind=='pg':
            async with self.pool.acquire() as c:
                await c.execute('INSERT INTO p32_states VALUES($1,$2) ON CONFLICT DO NOTHING',self.key(run_id),raw(initial()))
        else:
            async with self.pool.acquire() as c:
                async with c.cursor() as q:await q.execute('INSERT IGNORE INTO p32_states VALUES(%s,%s)',(self.key(run_id),raw(initial())))
                await c.commit()
    async def apply(self,run_id,request):
        assert run_id==request['run_id']
        started=time.perf_counter();key=self.key(run_id);eid=self.key(run_id+'/'+request['id'])
        if self.kind=='pg':
            async with self.pool.acquire() as c:
                acquired=time.perf_counter()
                tx=c.transaction();await tx.start()
                try:
                    if self.serial:await c.fetchval('SELECT body FROM p32_states WHERE id=$1 FOR UPDATE',self.key('__workspace_lock__'))
                    state=json.loads(await c.fetchval('SELECT body FROM p32_states WHERE id=$1 FOR UPDATE',key))
                    locked=time.perf_counter()
                    old=await c.fetchval('SELECT body FROM p32_entries WHERE id=$1',eid)
                    updated,entry,result=transition(state,request,json.loads(old) if old else None)
                    if updated:
                        await c.execute('UPDATE p32_states SET body=$1 WHERE id=$2',raw(updated),key)
                        await c.execute('INSERT INTO p32_entries VALUES($1,$2,$3)',eid,key,raw(entry))
                    written=time.perf_counter()
                    if getattr(self,'fail_before_commit',False):raise OSError('INJECTED_COMMIT_FAILURE')
                    await tx.commit()
                except BaseException:
                    await tx.rollback();raise
        else:
            async with self.pool.acquire() as c:
                acquired=time.perf_counter();await c.begin()
                try:
                    async with c.cursor() as q:
                        if self.serial:
                            await q.execute('SELECT body FROM p32_states WHERE id=%s FOR UPDATE',(self.key('__workspace_lock__'),));await q.fetchone()
                        await q.execute('SELECT body FROM p32_states WHERE id=%s FOR UPDATE',(key,));state=json.loads((await q.fetchone())[0]);locked=time.perf_counter()
                        await q.execute('SELECT body FROM p32_entries WHERE id=%s',(eid,));row=await q.fetchone()
                        updated,entry,result=transition(state,request,json.loads(row[0]) if row else None)
                        if updated:
                            await q.execute('UPDATE p32_states SET body=%s WHERE id=%s',(raw(updated),key))
                            await q.execute('INSERT INTO p32_entries VALUES(%s,%s,%s)',(eid,key,raw(entry)))
                    written=time.perf_counter()
                    if getattr(self,'fail_before_commit',False):raise OSError('INJECTED_COMMIT_FAILURE')
                    await c.commit()
                except BaseException:
                    await c.rollback();raise
        ended=time.perf_counter()
        result['timing']={'pool_ms':(acquired-started)*1000,'lock_read_ms':(locked-acquired)*1000,
                          'transition_write_ms':(written-locked)*1000,'commit_ms':(ended-written)*1000,'retries':0}
        return result
    async def state(self,run_id):
        if self.kind=='pg':
            async with self.pool.acquire() as c:return json.loads(await c.fetchval('SELECT body FROM p32_states WHERE id=$1',self.key(run_id)))
        async with self.pool.acquire() as c:
            async with c.cursor() as q:
                await q.execute('SELECT body FROM p32_states WHERE id=%s',(self.key(run_id),));row=await q.fetchone()
            await c.rollback();return json.loads(row[0])
    async def entries(self,run_id):
        if self.kind=='pg':
            async with self.pool.acquire() as c:rows=await c.fetch('SELECT body FROM p32_entries WHERE run_id=$1',self.key(run_id))
        else:
            async with self.pool.acquire() as c:
                async with c.cursor() as q:
                    await q.execute('SELECT body FROM p32_entries WHERE run_id=%s',(self.key(run_id),));rows=await q.fetchall()
                await c.rollback()
        return [json.loads(r[0]) for r in rows]
    async def close(self):
        if self.kind=='pg':await self.pool.close()
        else:self.pool.close();await self.pool.wait_closed()

class EtcdStore:
    def __init__(self,scope):self.scope=scope
    async def start(self):
        import aetcd
        self.client=aetcd.Client(host='127.0.0.1',port=15442,timeout=30)
        await self.client.connect()
        try:await self.client.get(b'/p32-health')
        except BaseException:
            await self.client.close();raise
        self.slots=asyncio.Semaphore(32)
        return self
    def key(self,key):return ('/p32/'+self.scope+'/'+key).encode()
    async def create(self,run_id):
        key=self.key('s/'+run_id);t=self.client.transactions
        await self.client.transaction([t.version(key)==0],[t.put(key,raw(initial()).encode())],[])
    async def apply(self,run_id,request):
        assert run_id==request['run_id']
        started=time.perf_counter();read_ms=write_ms=0;retries=0
        sk=self.key('s/'+run_id);ek=self.key('e/'+run_id+'/'+request['id']);t=self.client.transactions
        async with self.slots:
            acquired=time.perf_counter()
            while True:
                before=time.perf_counter()
                _,snap=await self.client.transaction([],[t.get(sk),t.get(ek)],[])
                read_ms+=(time.perf_counter()-before)*1000
                data,meta=snap[0][0];old=snap[1]
                updated,entry,result=transition(json.loads(data),request,json.loads(old[0][0]) if old else None)
                if not updated:break
                before=time.perf_counter()
                if getattr(self,'fail_before_commit',False):raise OSError('INJECTED_COMMIT_FAILURE')
                ok,_=await self.client.transaction([t.mod(sk)==meta.mod_revision,t.version(ek)==0],
                    [t.put(sk,raw(updated).encode()),t.put(ek,raw(entry).encode())],[])
                write_ms+=(time.perf_counter()-before)*1000
                if ok:break
                retries+=1
                if retries>=1000 or time.perf_counter()-started>30:raise TimeoutError('CAS_RETRY_BUDGET')
                await asyncio.sleep(min(.0005*2**min(retries,6),.02))
        result['timing']={'pool_ms':(acquired-started)*1000,'read_ms':read_ms,'cas_commit_ms':write_ms,'retries':retries}
        return result
    async def state(self,run_id):return json.loads((await self.client.get(self.key('s/'+run_id))).value)
    async def entries(self,run_id):return [json.loads(v.value) for v in await self.client.get_prefix(self.key('e/'+run_id+'/'))]
    async def close(self):await self.client.close()

async def connect(kind,scope):
    if kind in {"sqlite-wal","lmdb-txn"}:
        from .embedded_client import EmbeddedClient
        return await EmbeddedClient(kind,scope).start()
    store=EtcdStore(scope) if kind=='etcd-cas' else SQLStore('mysql' if kind=='mysql-row' else 'pg',scope,serial=kind=='pg-workspace')
    return await store.start()
