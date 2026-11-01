"""Agent logs on demand (B13).

An admin asks for a node's log, the next heartbeat tells the agent to upload
the tail of its own log file, and the upload lands in the object store. The
node row keeps only the timestamps and the key of the latest upload.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.errors import AppError
from app.models import Node
from app.storage import storage

MAX_UPLOAD_BYTES = 1024 * 1024
# A request the agent never answered (it restarted, the upload failed) goes out again.
RESEND_AFTER = timedelta(minutes=2)


def request_logs(node: Node, now: datetime) -> None:
    node.logs_requested_at = now
    node.logs_request_sent_at = None


def pending_requests(node: Node, now: datetime) -> list[str]:
    """The `requests` for a heartbeat response; marks the request as sent."""
    asked = node.logs_requested_at
    if asked is None or (node.logs_uploaded_at is not None and node.logs_uploaded_at >= asked):
        return []
    sent = node.logs_request_sent_at
    if sent is not None and now - sent < RESEND_AFTER:
        return []
    node.logs_request_sent_at = now
    return ["upload_logs"]


def store_upload(node: Node, body: bytes, now: datetime) -> str:
    if len(body) > MAX_UPLOAD_BYTES:
        raise AppError(413, "resource", "log upload is too large", limit_bytes=MAX_UPLOAD_BYTES)
    key = f"nodes/{node.id}/agent-log-{now:%Y%m%dT%H%M%S}.txt"
    storage.put_bytes(key, body, "text/plain")
    previous = node.logs_object_key
    node.logs_object_key = key
    node.logs_uploaded_at = now
    if previous and previous != key:
        try:
            storage.client.remove_object(storage.bucket, previous)
        except Exception:  # an orphaned old log is harmless
            pass
    return key


def read_logs(node: Node) -> dict:
    text = None
    if node.logs_object_key:
        try:
            text = storage.get_bytes(node.logs_object_key).decode("utf-8", errors="replace")
        except Exception:
            text = None
    return {
        "requested_at": node.logs_requested_at.isoformat() if node.logs_requested_at else None,
        "uploaded_at": node.logs_uploaded_at.isoformat() if node.logs_uploaded_at else None,
        "text": text,
    }
