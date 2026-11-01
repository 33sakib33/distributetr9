from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics, usage
from app.config import settings
from app.db import get_session
from app.deps import get_current_user, require_admin
from app.errors import AppError
from app.models import User
from app.observability import current_user, structured
from app.ratelimit import limit_user_or_ip
from app.schemas import ClientErrorIn
from app.scheduler import db_now

router = APIRouter()
client_log = logging.getLogger("lab.client")


def _csv(rows: list[dict], name: str) -> Response:
    return Response(
        content=usage.to_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/admin/usage")
async def admin_usage(
    start: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    group: str = "user",
    format: str = "json",
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    now = await db_now(session)
    begin, stop = usage.window(start, to, now)
    rows = await usage.usage_rows(session, group=group, begin=begin, stop=stop, now=now)
    if format == "csv":
        return _csv(rows, f"usage-by-{group}.csv")
    return {
        "from": begin.isoformat(),
        "to": stop.isoformat(),
        "group": group,
        "rows": rows,
        "lab": await usage.lab_usage(session, begin=begin, stop=stop, now=now),
    }


@router.get("/me/usage")
async def my_usage(
    start: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    format: str = "json",
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    now = await db_now(session)
    begin, stop = usage.window(start, to, now)
    rows = await usage.usage_rows(session, group="project", begin=begin, stop=stop, now=now, user_id=user.id)
    if format == "csv":
        return _csv(rows, "my-usage.csv")
    return {"from": begin.isoformat(), "to": stop.isoformat(), "group": "project", "rows": rows}


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    token = settings.metrics_token
    if token:
        presented = authorization.split(" ", 1)[1].strip() if authorization and authorization.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(presented.encode(), token.encode()):
            raise AppError(401, "auth", "metrics token required")
    return Response(content=await metrics.render(session), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.post(
    "/client-errors",
    status_code=202,
    dependencies=[Depends(limit_user_or_ip("client_errors", "rate_limit_client_errors_per_minute"))],
)
async def client_error(body: ClientErrorIn) -> dict:
    """The portal reports an error a user hit in the browser (F19). Anyone may
    send one, so it only becomes a log line and a count."""
    metrics.CLIENT_ERRORS.inc()
    structured(
        client_log,
        "client error: " + body.message[:200],
        logging.WARNING,
        message=body.message,
        stack=body.stack,
        url=body.url,
        user_agent=body.user_agent,
        user=current_user(),
    )
    return {"logged": True}
