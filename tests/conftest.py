from __future__ import annotations

import os

# Must run before `app.config` is imported.
os.environ["DATABASE_URL"] = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://lab:lab@127.0.0.1:5433/lab"
)
os.environ["MINIO_ENDPOINT"] = os.environ.get("MINIO_ENDPOINT", "127.0.0.1:9000")
os.environ["MINIO_ACCESS_KEY"] = os.environ.get("MINIO_ACCESS_KEY", "labminio")
os.environ["MINIO_SECRET_KEY"] = os.environ.get("MINIO_SECRET_KEY", "labminio")
os.environ["MINIO_BUCKET"] = os.environ.get("MINIO_BUCKET", "lab")
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
