from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings
from app.errors import AppError
from app.session_tokens import _b64

router = APIRouter()
_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_codes: dict[str, dict[str, Any]] = {}
KID = "lab-dev-oidc"


def _issuer() -> str:
    return settings.issuer_url() or "http://127.0.0.1:8080/oidc"


def _public_jwk() -> dict[str, str]:
    pub = _private_key.public_key().public_numbers()
    n = _b64(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big"))
    e = _b64(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": n, "e": e}


def discovery_doc() -> dict[str, Any]:
    issuer = _issuer()
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["openid", "email", "profile"],
    }


def public_key():
    return _private_key.public_key()


def issue_tokens(
    *,
    code: str,
    verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    from app.errors import AppError
    from app.session_tokens import pkce_challenge

    row = _codes.pop(code, None)
    if (
        row is None
        or time.time() > float(row["exp"])
        or client_id != settings.oidc_client_id
        or redirect_uri != settings.oidc_redirect_uri
    ):
        raise AppError(401, "auth", "university login failed (token exchange)")
    if settings.oidc_client_secret and client_secret != settings.oidc_client_secret:
        raise AppError(401, "auth", "university login failed (token exchange)")
    if row.get("challenge") and pkce_challenge(verifier) != row["challenge"]:
        raise AppError(401, "auth", "university login failed (token exchange)")

    now = int(time.time())
    issuer = _issuer()
    claims = {
        "iss": issuer,
        "sub": f"devoidc:{row['email']}",
        "aud": settings.oidc_client_id,
        "iat": now,
        "exp": now + 300,
        "nonce": row["nonce"],
        "email": row["email"],
        "email_verified": True,
        "name": row["name"],
    }
    token = jwt.encode(claims, _private_key, algorithm="RS256", headers={"kid": KID})
    return {
        "access_token": secrets.token_urlsafe(16),
        "token_type": "Bearer",
        "id_token": token,
        "expires_in": 300,
    }


@router.get("/.well-known/openid-configuration")
async def discovery() -> dict[str, Any]:
    return discovery_doc()


@router.get("/jwks")
async def jwks() -> dict[str, Any]:
    return {"keys": [_public_jwk()]}


@router.get("/authorize")
async def authorize(
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    nonce: str = "",
    code_challenge: str = "",
) -> HTMLResponse:
    error = ""
    if client_id != settings.oidc_client_id:
        error = "Unknown application."
    if redirect_uri != settings.oidc_redirect_uri:
        error = "Redirect URI does not match this lab."
    q = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
        }
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>University sign-in</title>
  <style>
    body {{ font-family: Palatino, "Times New Roman", serif; background: #14110e; color: #f3ece3;
           display: grid; min-height: 100vh; place-items: center; margin: 0; }}
    form, .card {{ background: #1d1914; border: 1px solid #3a3229; border-radius: 10px;
                   padding: 28px; width: min(420px, 92vw); display: grid; gap: 12px; }}
    h1 {{ font-size: 22px; margin: 0; }}
    p {{ color: #b7ab9c; margin: 0; }}
    label {{ display: grid; gap: 6px; font-size: 14px; color: #b7ab9c; }}
    input {{ background: #0f0d0b; color: #f3ece3; border: 1px solid #3a3229; border-radius: 6px;
             padding: 8px 10px; font: inherit; }}
    button {{ background: #d8a15a; color: #1a140e; border: 0; border-radius: 6px;
              padding: 10px 14px; font: inherit; font-weight: 600; cursor: pointer; }}
    .err {{ color: #d37a6a; }}
  </style>
</head>
<body>
  <form method="post" action="/oidc/authorize?{q}">
    <h1>University sign-in</h1>
    <p>Local campus IdP for this lab. Point <code>OIDC_ISSUER</code> at Entra, Google, or Keycloak when IT gives you an app.</p>
    {"<p class='err'>" + error + "</p>" if error else ""}
    <label>University email
      <input name="email" type="email" value="alice@uni.edu" required {"disabled" if error else ""}/>
    </label>
    <label>Name
      <input name="name" value="Alice"/>
    </label>
    <button type="submit" {"disabled" if error else ""}>Sign in</button>
  </form>
</body>
</html>"""
    return HTMLResponse(html)


@router.post("/authorize")
async def authorize_post(
    email: str = Form(),
    name: str = Form(default=""),
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    nonce: str = "",
    code_challenge: str = "",
) -> RedirectResponse:
    if client_id != settings.oidc_client_id or redirect_uri != settings.oidc_redirect_uri:
        return RedirectResponse("/oidc/authorize", status_code=303)
    email = email.strip().lower()
    if "@" not in email:
        return RedirectResponse("/oidc/authorize", status_code=303)
    code = secrets.token_urlsafe(24)
    _codes[code] = {
        "email": email,
        "name": (name or email.split("@")[0]).strip(),
        "nonce": nonce,
        "challenge": code_challenge,
        "exp": time.time() + 300,
    }
    return RedirectResponse(redirect_uri + "?" + urlencode({"code": code, "state": state}), status_code=302)


@router.post("/token")
async def token(
    grant_type: str = Form(default=""),
    code: str = Form(default=""),
    client_id: str = Form(default=""),
    client_secret: str = Form(default=""),
    redirect_uri: str = Form(default=""),
    code_verifier: str = Form(default=""),
) -> JSONResponse:
    if grant_type != "authorization_code":
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    try:
        body = issue_tokens(
            code=code,
            verifier=code_verifier,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    except AppError:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    return JSONResponse(body)
