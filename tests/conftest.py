from __future__ import annotations

import asyncio
import os

import asyncpg

# Tests run against their own database and bucket, recreated each session, so
# they never write into the dev data that `docker compose` keeps.
_BASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://lab:lab@127.0.0.1:5433/lab")
_TEST_DB = os.environ.get("TEST_DATABASE_NAME", "lab_test")


def _with_db(url: str, name: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + name


async def _recreate_test_db() -> None:
    admin = _with_db(_BASE_URL, "postgres").replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    finally:
        await conn.close()


asyncio.run(_recreate_test_db())

# Must run before `app.config` is imported.
os.environ["DATABASE_URL"] = _with_db(_BASE_URL, _TEST_DB)
os.environ["MINIO_ENDPOINT"] = os.environ.get("MINIO_ENDPOINT", "127.0.0.1:9000")
os.environ["MINIO_ACCESS_KEY"] = os.environ.get("MINIO_ACCESS_KEY", "labminio")
os.environ["MINIO_SECRET_KEY"] = os.environ.get("MINIO_SECRET_KEY", "labminio")
os.environ["MINIO_BUCKET"] = os.environ.get("TEST_MINIO_BUCKET", "lab-test")
os.environ["MINIO_SECURE"] = "false"
os.environ["DEV_AUTH"] = "1"
os.environ["DEV_AUTH_ADMIN"] = "1"
os.environ["CLAIM_POLL_SECONDS"] = "0"
os.environ["LEASE_TTL_SECONDS"] = "2"
os.environ["DISCONNECT_GRACE_SECONDS"] = "1"
os.environ["HEARTBEAT_INTERVAL_SECONDS"] = "60"
os.environ["DISK_WATERMARK_BYTES"] = "0"
os.environ["DEFAULT_USER_CONCURRENCY"] = "50"
os.environ["DEFAULT_PROJECT_CONCURRENCY"] = "50"
os.environ["TOKEN_PEPPER"] = "test-token-pepper"
os.environ["STRICT_NODE_CLASS"] = "1"
os.environ["REGISTRY_URL"] = os.environ.get("REGISTRY_URL", "127.0.0.1:5000")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client
