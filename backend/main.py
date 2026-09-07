from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastapi import APIRouter

from app.db.database import init_db
from app.api.os import models, agents, events, subscriptions, connectors, mcp_servers, skills, mcp_registry, a2a
from app.api.apps import chat as chat_app
from app.api.internal import e2ag_mcp
from app.services.cron_scheduler import cron_scheduler
from app.services.imap_poller import imap_poller
from app.services.event_retry_worker import retry_worker
from app.middleware.access_token import AccessTokenMiddleware
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await cron_scheduler.start()
    await imap_poller.start()
    retry_worker.start()
    yield
    retry_worker.stop()
    await imap_poller.stop()
    await cron_scheduler.stop()


app = FastAPI(title="DiOS", description="DiFlow Intelligent Operation System", lifespan=lifespan)

app.add_middleware(AccessTokenMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

os_router = APIRouter(prefix="/api/os")
os_router.include_router(models.router)
os_router.include_router(agents.router)
os_router.include_router(events.router)
os_router.include_router(subscriptions.router)
os_router.include_router(connectors.router)
os_router.include_router(mcp_servers.router)
os_router.include_router(skills.router)
os_router.include_router(mcp_registry.router)
os_router.include_router(a2a.router)
app.include_router(os_router)
app.include_router(e2ag_mcp.router, prefix="/api")

apps_router = APIRouter(prefix="/api/apps")
apps_router.include_router(chat_app.router)
app.include_router(apps_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/auth/status")
async def auth_status():
    """前端判断是否需展示 Access Token 登录页。"""
    return {"access_token_required": bool(settings.access_token)}
