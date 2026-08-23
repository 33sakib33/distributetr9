from __future__ import annotations

import hashlib
import secrets

from app.config import settings


def hash_token(token: str) -> str:
    return hashlib.sha256(f"{settings.token_pepper}:{token}".encode()).hexdigest()


def new_node_token() -> str:
    return secrets.token_urlsafe(32)


def new_boot_session() -> str:
    return secrets.token_hex(16)
