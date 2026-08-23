from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, init_db
from app.errors import AppError, app_error_handler
from app.routers import agent, auth, portal
from app.scheduler import expire_leases, mark_offline_nodes
from app.seed import seed
from app.storage import storage


async def _lease_worker() -> None:
    while True:
        try:
            async with SessionLocal() as session:
                await expire_leases(session)
                await mark_offline_nodes(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    storage.ensure_bucket()
    async with SessionLocal() as session:
        await seed(session)
    task = asyncio.create_task(_lease_worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Lab ML Job Control", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(portal.router)
app.include_router(agent.router)
if settings.oidc_dev:
    from app.oidc_dev import router as oidc_dev_router

    app.include_router(oidc_dev_router, prefix="/oidc")
