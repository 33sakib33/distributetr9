from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from app.config import settings
from app.errors import AppError

_discovery: dict[str, Any] | None = None
_jwks_clients: dict[str, PyJWKClient] = {}


def _dev_issuer() -> bool:
    issuer = settings.issuer_url()
    return bool(settings.oidc_dev and issuer.rstrip("/").endswith("/oidc"))


async def fetch_discovery() -> dict[str, Any]:
    global _discovery
    if _discovery is not None:
        return _discovery
    if _dev_issuer():
        from app import oidc_dev

        _discovery = oidc_dev.discovery_doc()
        return _discovery
    issuer = settings.issuer_url()
    if not issuer:
        raise AppError(503, "auth", "OIDC issuer is not configured")
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        _discovery = resp.json()
    return _discovery


def clear_discovery_cache() -> None:
    global _discovery
    _discovery = None
    _jwks_clients.clear()


def authorization_url(discovery: dict[str, Any], *, state: str, nonce: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return discovery["authorization_endpoint"] + "?" + urlencode(params)


async def exchange_code(discovery: dict[str, Any], *, code: str, verifier: str) -> dict[str, Any]:
    if _dev_issuer():
        from app import oidc_dev

        return oidc_dev.issue_tokens(
            code=code,
            verifier=verifier,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_uri,
        )
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "client_id": settings.oidc_client_id,
        "code_verifier": verifier,
    }
    if settings.oidc_client_secret:
        data["client_secret"] = settings.oidc_client_secret
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(discovery["token_endpoint"], data=data)
        if resp.status_code >= 400:
            raise AppError(401, "auth", "university login failed (token exchange)")
        return resp.json()


def decode_id_token(id_token: str, *, nonce: str, jwks_uri: str) -> dict[str, Any]:
    try:
        if _dev_issuer():
            from app import oidc_dev

            key = oidc_dev.public_key()
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=settings.oidc_client_id,
                issuer=settings.issuer_url(),
            )
        else:
            client = _jwks_clients.get(jwks_uri)
            if client is None:
                client = PyJWKClient(jwks_uri, cache_jwk_set=True, lifespan=3600)
                _jwks_clients[jwks_uri] = client
            key = client.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256"],
                audience=settings.oidc_client_id,
                issuer=settings.issuer_url(),
            )
    except jwt.InvalidTokenError as exc:
        raise AppError(401, "auth", "university login failed (id token)") from exc
    if claims.get("nonce") != nonce:
        raise AppError(401, "auth", "university login failed (nonce)")
    return claims
