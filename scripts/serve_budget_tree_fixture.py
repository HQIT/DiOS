"""Isolated API fixture: inherited tool ceiling with real ledger and E2AG gates."""
import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
from app.api.os import runtime_tasks
from app.services.task_console import TaskConsole
from app.services.task_inputs import CONSOLE_ORIGIN
from app.runtime.store import RuntimeStore
from test_runtime_budget_tree import SQLiteTreeTests

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, required=True)
args = parser.parse_args()
args.root.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app):
    f = SQLiteTreeTests()
    original=RuntimeStore.submit
    async def submit(store,key,value,**kw):
        return await original(store,key,dict(value,origin=CONSOLE_ORIGIN,task_kind='generic',
            payload={'goal':'验证父子任务共享预算','sample':{'id':'synthetic','name':'合成预算样例'}}),**kw)
    with patch.object(RuntimeStore,'submit',submit):
        await f.asyncSetUp()
    try:
        with patch.object(RuntimeStore,'submit',submit):
            child = await f.child()
        await f.activate(child)
        for key in ['first', 'second']:
            item = await f.invocation(f.run, f.binding, key)
            await f.gate.dispatch(f.run['id'], item['id'], item['version'])
        profiles=f.root/'profiles';profiles.mkdir()
        for profile in ['test-profile','child-profile']:
            (profiles/(profile+'.json')).write_text(json.dumps(dict(id=profile,
                bridge='jsonl-v1',governance={'mode':'e2ag'},
                launch={'image':'test-image','command':['test-command']},
                console={'enabled':True,'input_kind':'generic'})))
        app.state.runtime_console=TaskConsole(f.db,f.scope,profiles,f.provider.root,policy=f.policy)
        (args.root/'ready.json').write_text(json.dumps({'parent':f.run['id'],'child':child['id']}))
        with patch.object(runtime_tasks.settings,'runtime_console_enabled',True), patch.object(runtime_tasks.settings,'access_token','operator-test'):
            yield
    finally:
        await f.asyncTearDown()


app=FastAPI(lifespan=lifespan)
app.include_router(runtime_tasks.router,prefix='/dios/api/os')


@app.get('/dios/api/auth/status')
async def status():
    return {'access_token_required':True}


app.mount('/dios',StaticFiles(directory=ROOT/'frontend/dist',html=True))
uvicorn.run(app,host='127.0.0.1',port=13346,log_level='warning')
