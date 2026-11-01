"""Alembic environment. The app runs migrations on startup through
app.db.init_db, which hands us a live connection; the CLI builds its own."""
from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.models import Base

target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_cli() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    asyncio.run(_run_cli())
