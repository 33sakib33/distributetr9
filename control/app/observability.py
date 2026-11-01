"""Request IDs and structured logs (B30).

Every request gets an id: the caller's `X-Request-ID` when it is a sane value,
else a fresh one. It is echoed in the response, attached to every log line made
while the request runs, and one line per request records what happened. With
LOG_FORMAT=json each line is one JSON object, which is what a log shipper wants.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

REQUEST_ID_HEADER = "X-Request-ID"
_SANE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

# The request in flight: its id, plus who made it once authentication has run.
_current: ContextVar[dict | None] = ContextVar("lab_request", default=None)

request_log = logging.getLogger("lab.request")


def note(**fields: str) -> None:
    """Record who the request belongs to (called by the auth dependencies)."""
    current = _current.get()
    if current is not None:
        current.update(fields)


def current_request_id() -> str | None:
    current = _current.get()
    return current["request_id"] if current else None


def current_user() -> str | None:
    current = _current.get()
    return current["user"] if current else None


def new_request_id(incoming: str | None) -> str:
    if incoming and _SANE_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestLogMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware), so streaming responses such as live
    logs are not buffered and the context variable reaches the endpoint."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for name, value in scope["headers"]:
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        context = {"request_id": new_request_id(incoming), "user": None, "node": None}
        token = _current.set(context)
        started = time.perf_counter()
        status = 500

        async def send_with_id(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message["headers"] if k.lower() != b"x-request-id"]
                headers.append((REQUEST_ID_HEADER.lower().encode(), context["request_id"].encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_log.info(
                "%s %s %s",
                scope["method"],
                scope["path"],
                status,
                extra={
                    "fields": {
                        "request_id": context["request_id"],
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "user": context["user"],
                        "node": context["node"],
                    }
                },
            )
            _current.reset(token)


def structured(logger: logging.Logger, summary: str, level: int = logging.INFO, **fields) -> None:
    """A log line whose fields are named values, not text to parse."""
    logger.log(level, summary, extra={"fields": fields})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        fields = getattr(record, "fields", None)
        if fields:
            entry.update(fields)
        else:
            entry["message"] = record.getMessage()
        request_id = current_request_id()
        if request_id:
            entry.setdefault("request_id", request_id)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = getattr(record, "fields", None) or {}
        request_id = fields.get("request_id") or current_request_id()
        extra = " ".join(
            f"{k}={v}" for k, v in fields.items() if k not in {"method", "path", "status", "request_id"} and v is not None
        )
        return " ".join(part for part in (line, extra, f"request_id={request_id}" if request_id else "") if part)


def configure_logging(log_format: str) -> None:
    """Give the "lab" loggers one handler in the chosen format. Records still
    propagate, so a test or a host that configures the root logger sees them."""
    handler = logging.StreamHandler()
    handler.set_name("lab-structured")
    if (log_format or "text").strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    lab = logging.getLogger("lab")
    lab.handlers = [h for h in lab.handlers if h.get_name() != "lab-structured"] + [handler]
    lab.setLevel(logging.INFO)
    # The request line replaces uvicorn's access log; keeping both would log every request twice.
    logging.getLogger("uvicorn.access").disabled = True
