from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from app.config import settings

SESSION_PREFIX = "v1."
TICKET_TTL_SECONDS = 60
STATE_TTL_SECONDS = 600

_tickets: dict[str, dict[str, Any]] = {}


def _key() -> bytes:
    return settings.token_pepper.encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_payload(data: dict[str, Any]) -> str:
    body = _b64(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_payload(token: str, max_age: int | None = None) -> dict[str, Any]:
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed token") from exc
    expected = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("bad signature")
    data = json.loads(_unb64(body))
    ts = int(data.get("ts") or data.get("exp") or 0)
    if max_age is not None:
        if ts > 10**12:
            ts = ts // 1000
        issued = int(data.get("ts") or 0)
        if issued and time.time() - issued > max_age:
            raise ValueError("expired")
    exp = int(data.get("exp") or 0)
    if exp and time.time() > exp:
        raise ValueError("expired")
    return data


def issue_session(user_id: str) -> str:
    now = int(time.time())
    payload = {"uid": user_id, "ts": now, "exp": now + settings.session_hours * 3600}
    return SESSION_PREFIX + sign_payload(payload)


def read_session(token: str) -> str:
    if not token.startswith(SESSION_PREFIX):
        raise ValueError("not a session")
    data = verify_payload(token[len(SESSION_PREFIX) :])
    uid = data.get("uid")
    if not uid:
        raise ValueError("no user")
    return str(uid)


def issue_ticket(session_token: str) -> str:
    ticket = secrets.token_urlsafe(24)
    _tickets[ticket] = {"token": session_token, "exp": time.time() + TICKET_TTL_SECONDS}
    return ticket


def consume_ticket(ticket: str) -> str:
    row = _tickets.pop(ticket, None)
    if row is None or time.time() > float(row["exp"]):
        raise ValueError("invalid ticket")
    return str(row["token"])


def pkce_verifier() -> str:
    return secrets.token_urlsafe(48)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return _b64(digest)
