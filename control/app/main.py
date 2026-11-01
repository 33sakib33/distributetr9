from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.alerts import Notifier, run_cycle
from app.config import settings
from app.db import SessionLocal, init_db
from app.envbuild import ensure_mirror_builds
from app.errors import AppError, app_error_handler
from app.notify import JobNotifier
from app.observability import RequestLogMiddleware, configure_logging
from app.routers import admin, agent, auth, lab, people, portal, reports, student
from app.scheduler import db_now, expire_leases, mark_offline_nodes
from app.seed import seed
from app.storage import storage
from app.telemetry import prune_health, thin_usage


async def _alert_worker() -> None:
    notifier = Notifier()
    while True:
        # Wait first: a restart should not re-announce before the PCs have had time to reconnect.
        await asyncio.sleep(settings.alert_check_seconds)
        try:
            async with SessionLocal() as session:
                await run_cycle(session, notifier)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("lab").exception("alert check failed")


async def _notify_worker() -> None:
    notifier = JobNotifier()
    while True:
        await asyncio.sleep(settings.notify_check_seconds)
        try:
            async with SessionLocal() as session:
                await notifier.run_once(session, await db_now(session))
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("lab").exception("job notifications failed")


MIRROR_CHECK_SECONDS = 600
# Thinning finished attempts' samples and pruning old health rows is housekeeping.
TELEMETRY_TRIM_SECONDS = 300


async def _trim_telemetry() -> None:
    try:
        async with SessionLocal() as session:
            await thin_usage(session)
            await prune_health(session, await db_now(session))
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger("lab").exception("could not trim telemetry")


async def _queue_mirror_builds() -> None:
    try:
        async with SessionLocal() as session:
            await ensure_mirror_builds(session)
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger("lab").exception("could not queue catalog mirror builds")


async def _lease_worker() -> None:
    last_mirror_check = last_trim = time.monotonic()
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
        if time.monotonic() - last_mirror_check >= MIRROR_CHECK_SECONDS:
            last_mirror_check = time.monotonic()
            await _queue_mirror_builds()
        if time.monotonic() - last_trim >= TELEMETRY_TRIM_SECONDS:
            last_trim = time.monotonic()
            await _trim_telemetry()
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.environment == "production":
        problems = settings.production_problems()
        if problems:
            raise RuntimeError("refusing to start in production:\n  - " + "\n  - ".join(problems))
    await init_db()
    storage.ensure_bucket()
    async with SessionLocal() as session:
        await seed(session)
    await _queue_mirror_builds()
    tasks = [
        asyncio.create_task(_lease_worker()),
        asyncio.create_task(_alert_worker()),
        asyncio.create_task(_notify_worker()),
    ]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


configure_logging(settings.log_format)
app = FastAPI(title="Lab ML Job Control", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLogMiddleware)
app.include_router(auth.router)
app.include_router(student.router)
app.include_router(portal.router)
app.include_router(admin.router)
app.include_router(people.router)
app.include_router(lab.router)
app.include_router(agent.router)
app.include_router(reports.router)
if settings.oidc_dev:
    from app.oidc_dev import router as oidc_dev_router

    app.include_router(oidc_dev_router, prefix="/oidc")
