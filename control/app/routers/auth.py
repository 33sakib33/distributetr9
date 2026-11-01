from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.db import SessionLocal
from app.errors import AppError
from app.ratelimit import limit_ip
from app.identity import email_allowed, upsert_oidc_user
from app import oidc
from app.session_tokens import (
    STATE_TTL_SECONDS,
    issue_session,
    issue_ticket,
    consume_ticket,
    pkce_challenge,
    pkce_verifier,
    sign_payload,
    verify_payload,
)

router = APIRouter()


def _portal() -> str:
    return settings.portal_base_url.rstrip("/")


def _safe_next(url: str | None) -> str:
    portal = _portal()
    if not url:
        return portal + "/"
    parsed = urlparse(url)
    allowed = urlparse(portal)
    if parsed.scheme in ("http", "https") and parsed.netloc == allowed.netloc:
        return url
    if url.startswith("/") and not url.startswith("//"):
        return portal + url
    return portal + "/"


def _login_error(message: str) -> RedirectResponse:
    return RedirectResponse(_portal() + "/login?" + urlencode({"error": message}), status_code=302)


def _set_session_cookie(response: RedirectResponse | JSONResponse, token: str) -> None:
    response.set_cookie(
        "lab_session",
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_hours * 3600,
        path="/",
        secure=settings.session_secure,
    )


@router.get("/auth/config")
async def auth_config() -> dict:
    parsed = urlparse(settings.oidc_redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else settings.public_base_url.rstrip("/")
    campus = settings.campus_login()
    return {
        "local_login": settings.dev_auth,
        "campus_login": campus,
        "login_url": f"{origin}/auth/login" if campus else None,
        "oidc_enabled": settings.oidc_enabled(),
        "dev_auth": settings.dev_auth,
    }


@router.post("/auth/local", dependencies=[Depends(limit_ip("login", "rate_limit_login_per_minute"))])
async def auth_local(request: Request) -> JSONResponse:
    if not settings.dev_auth:
        raise AppError(401, "auth", "Please sign in")
    body = await request.json()
    email = str(body.get("email") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    if "@" not in email:
        raise AppError(400, "auth", "Enter an email address")
    if not email_allowed(email):
        raise AppError(403, "auth", "That email is not allowed")
    async with SessionLocal() as db:
        user = await upsert_oidc_user(db, sub=f"local:{email}", email=email, name=name)
        if user.disabled_at is not None:
            await db.commit()
            raise AppError(403, "disabled", "This account is disabled")
        await db.commit()
        token = issue_session(str(user.id))
    resp = JSONResponse({"token": token, "email": user.email, "display_name": user.display_name})
    _set_session_cookie(resp, token)
    return resp


@router.get("/auth/login", dependencies=[Depends(limit_ip("login", "rate_limit_login_per_minute"))])
async def auth_login(next: str = "/") -> RedirectResponse:
    if not settings.oidc_enabled():
        return _login_error("University login is not configured.")
    try:
        discovery = await oidc.fetch_discovery()
    except Exception:
        return _login_error("University sign-in is unavailable. Existing sessions still work.")
    verifier = pkce_verifier()
    nonce = secrets.token_urlsafe(16)
    state = sign_payload(
        {
            "ts": int(time.time()),
            "next": _safe_next(next),
            "nonce": nonce,
            "verifier": verifier,
        }
    )
    url = oidc.authorization_url(
        discovery,
        state=state,
        nonce=nonce,
        challenge=pkce_challenge(verifier),
    )
    return RedirectResponse(url, status_code=302)


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    if error:
        return _login_error(error_description or error or "University login was cancelled.")
    if not code or not state:
        return _login_error("University login failed (missing code).")
    try:
        payload = verify_payload(state, max_age=STATE_TTL_SECONDS)
    except ValueError:
        return _login_error("University login expired. Try again.")

    try:
        discovery = await oidc.fetch_discovery()
        tokens = await oidc.exchange_code(
            discovery, code=code, verifier=str(payload["verifier"])
        )
        claims = oidc.decode_id_token(
            tokens["id_token"],
            nonce=str(payload["nonce"]),
            jwks_uri=str(discovery["jwks_uri"]),
        )
    except AppError as exc:
        return _login_error(exc.message)
    except Exception:
        return _login_error("University sign-in is unavailable. Existing sessions still work.")

    email = str(claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if "@" not in email:
        return _login_error("University account has no email.")
    if not email_allowed(email):
        return _login_error("That email domain is not allowed on this lab.")
    name = str(claims.get("name") or "").strip()
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        return _login_error("University account is missing a subject.")
    issuer = settings.issuer_url()
    university_sub = f"{issuer}::{sub}"

    async with SessionLocal() as db:
        user = await upsert_oidc_user(db, sub=university_sub, email=email, name=name)
        if user.disabled_at is not None:
            await db.commit()
            return _login_error("This account is disabled.")
        await db.commit()
        token = issue_session(str(user.id))

    nxt = _safe_next(str(payload.get("next") or "/"))
    ticket = issue_ticket(token)
    complete = _portal() + "/auth/complete?" + urlencode({"ticket": ticket, "next": nxt})
    response = RedirectResponse(complete, status_code=302)
    _set_session_cookie(response, token)
    return response


@router.post("/auth/exchange")
async def auth_exchange(request: Request) -> JSONResponse:
    body = await request.json()
    ticket = str(body.get("ticket") or "")
    try:
        token = consume_ticket(ticket)
    except ValueError:
        raise AppError(401, "auth", "login ticket expired")
    resp = JSONResponse({"token": token})
    _set_session_cookie(resp, token)
    return resp


@router.get("/auth/logout")
async def auth_logout(next: str = "/login") -> RedirectResponse:
    response = RedirectResponse(_safe_next(next), status_code=302)
    response.delete_cookie("lab_session", path="/")
    return response
