"""Sim control plane entrypoint: make sure the sim database exists, then serve."""
import asyncio
import os

import asyncpg


async def ensure_database() -> None:
    base, name = os.environ["DATABASE_URL"].rsplit("/", 1)
    conn = await asyncpg.connect(base.replace("+asyncpg", "") + "/postgres")
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


asyncio.run(ensure_database())
os.execvp("uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"])
