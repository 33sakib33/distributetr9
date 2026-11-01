"""Run a coroutine against the test database on its own engine (the app's
engine belongs to the test client's event loop)."""
from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def with_session(fn):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()
