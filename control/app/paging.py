"""Opaque keyset cursors for the list endpoints (newest first by created_at, id)."""
from __future__ import annotations

import base64
import uuid
from datetime import datetime

from app.errors import AppError


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, row_id = raw.split("|", 1)
        return datetime.fromisoformat(stamp), uuid.UUID(row_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(400, "invalid", "bad cursor") from exc



def encode_offset(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o|{offset}".encode()).decode().rstrip("=")


def decode_offset(cursor: str) -> int:
    """For small lists that have no creation time to key on (quotas)."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        tag, value = raw.split("|", 1)
        offset = int(value)
        if tag != "o" or offset < 0:
            raise ValueError("bad offset")
        return offset
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(400, "invalid", "bad cursor") from exc
