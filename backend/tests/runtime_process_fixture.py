"""Synthetic process fixture; no Agent, model, or business repository access."""
import asyncio,json,os,socket,sys
from uuid import uuid4
from app.runtime.database import Database
from app.runtime.store import RuntimeStore

async def main():
    db=Database(os.environ['DIOS_FIXTURE_URL']);store=RuntimeStore(db,os.environ['DIOS_FIXTURE_SCOPE'])
    try:
        mode=sys.argv[1]
        if mode=='claim':
            wid,instance=str(uuid4()),str(uuid4())
            await store.register_worker(wid,instance,socket.gethostname(),os.getpid())
            a=await store.claim(wid,instance)
            print(json.dumps({'pid':os.getpid(),'attempt':a}),flush=True)
        elif mode=='committed-crash':
            r=await store.submit('survive-crash',{'fixture':True})
            print(r['id'],flush=True)
            os._exit(77)
        elif mode=='uncommitted-crash':
            async with db.transaction(store.scope) as c:
                r=await store._run(c,sys.argv[2])
                await store._event(c,r,'fixture.uncommitted',{},await db.now(c),state='failed')
                os._exit(78)
    finally:await db.close()

if __name__=='__main__':asyncio.run(main())
