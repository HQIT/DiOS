"""Same bounded network gateway for both embedded candidates."""
import asyncio,json,time
from .model import raw

class EmbeddedClient:
    def __init__(self,kind,scope):self.kind=kind;self.scope=scope;self.port=15443 if kind=='sqlite-wal' else 15444
    async def connection(self):return await asyncio.open_connection('127.0.0.1',self.port,limit=128*1024*1024)
    async def start(self):
        self.pool=asyncio.Queue()
        try:
            for _ in range(32):self.pool.put_nowait(await self.connection())
            await self.call("health","health")
        except BaseException:
            await self.close();raise
        return self
    async def call(self,op,run_id,request=None):
        started=time.perf_counter();reader,writer=await self.pool.get();acquired=time.perf_counter();healthy=True
        try:
            q={'op':op,'scope':self.scope,'run_id':run_id,'request':request or {},'fail_before_commit':getattr(self,'fail_before_commit',False)}
            writer.write((raw(q)+'\n').encode());await writer.drain()
            line=await reader.readline()
            if not line:raise ConnectionError('embedded server disconnected')
            answer=json.loads(line)
            if answer.get('error'):raise OSError(answer['message'])
            result=answer['result']
            if op=='apply':result['timing']['pool_ms']=(acquired-started)*1000
            return result
        except BaseException:
            healthy=False;writer.close()
            raise
        finally:
            if healthy:self.pool.put_nowait((reader,writer))
            else:
                # Replace only the connection. The caller reconciles the invocation identity.
                try:self.pool.put_nowait(await self.connection())
                except Exception:pass
    async def create(self,run_id):return await self.call('create',run_id)
    async def apply(self,run_id,request):return await self.call('apply',run_id,request)
    async def state(self,run_id):return await self.call('state',run_id)
    async def entries(self,run_id):return await self.call('entries',run_id)
    async def close(self):
        while not self.pool.empty():
            _,writer=self.pool.get_nowait();writer.close()
            try:await writer.wait_closed()
            except Exception:pass
