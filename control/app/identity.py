from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User
from app.seed import ensure_user_quota


def _admin_emails() -> set[str]:
    return {part.strip().lower() for part in settings.admin_emails.split(",") if part.strip()}


def email_allowed(email: str) -> bool:
    domains = [
        part.strip().lower().lstrip("@")
        for part in settings.oidc_allowed_email_domains.split(",")
        if part.strip()
    ]
    if not domains:
        return True
    host = email.rsplit("@", 1)[-1].lower()
    return host in domains


async def upsert_oidc_user(
    session: AsyncSession,
    *,
    sub: str,
    email: str,
    name: str,
) -> User:
    email = email.strip().lower()
    display = (name or "").strip() or email.split("@")[0]
    university_sub = sub.strip()

    user = await session.scalar(select(User).where(User.university_sub == university_sub))
    if user is None:
        user = await session.scalar(select(User).where(User.email == email))
        if user is not None:
            user.university_sub = university_sub
        else:
            user = User(
                university_sub=university_sub,
                email=email,
                display_name=display,
                is_admin=email in _admin_emails(),
            )
            session.add(user)
            await session.flush()
            await ensure_user_quota(session, user)

    user.email = email
    if display:
        user.display_name = display
    if email in _admin_emails() and not user.is_admin:
        user.is_admin = True
    await session.flush()
    return user
