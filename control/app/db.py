from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
BASELINE_REVISION = "0001"

# Columns that were patched in with ALTER TABLE before migrations existed.
# A database created that way gets them, then is stamped at the baseline.
_LEGACY_PATCHES = [
    "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS spec TEXT",
    "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS compat_class TEXT",
    "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS error_message TEXT",
    "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS claimed_node_id UUID",
    "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
    "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'train'",
]


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def _upgrade(connection) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["connection"] = connection
    tables = set(inspect(connection).get_table_names())
    if "jobs" in tables and "alembic_version" not in tables:
        for stmt in _LEGACY_PATCHES:
            connection.execute(text(stmt))
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
