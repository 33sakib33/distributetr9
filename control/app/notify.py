"""Tell users when their job finishes (B26): e-mail through SMTP and, if the
user set one, a JSON POST to their webhook.

The worker looks at jobs that ended in the last 24 hours and have not been
announced. A job is marked `notified_at` once its channels are done (or there
is nothing to send); a failed delivery is counted in `notify_tries` and tried
again on the next pass, up to NOTIFY_MAX_TRIES times. Senders are injected so
tests never touch the network.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import smtplib
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import unquote, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.failure_explain import explain_failure, log_tail
from app.models import Job, Project, User
from app.status import derived_status, latest_attempt

log = logging.getLogger("lab")

RECENT = timedelta(hours=24)
WEBHOOK_TIMEOUT_SECONDS = 5

EmailSender = Callable[[str, str, str], Awaitable[None]]  # to, subject, body
WebhookPoster = Callable[[str, dict], Awaitable[None]]


def email_enabled() -> bool:
    return bool(settings.smtp_host.strip())


def _smtp_send(to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


async def send_email(to: str, subject: str, body: str) -> None:
    await asyncio.to_thread(_smtp_send, to, subject, body)


# --- Webhook destinations (X5) -----------------------------------------------------
# A user picks the URL and the server sends the request, so a webhook must not be a
# way to reach the lab's own network: loopback, private, link-local (the cloud
# metadata address 169.254.169.254 included), multicast and unspecified addresses
# are refused unless NOTIFY_ALLOW_PRIVATE_WEBHOOKS=1.

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def address_is_blocked(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or not address.is_global
    )


def _literal_address(host: str) -> IPAddress | None:
    """The address a host name spells out by itself (also the short and number
    forms such as 127.1 and 2130706433 that resolvers accept), else None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except (OSError, ValueError):
        return None


def webhook_problem(url: str) -> str | None:
    """Why a webhook URL is refused without looking anything up, or None."""
    if settings.notify_allow_private_webhooks:
        return None
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        return "the webhook needs a host name"
    if host == "localhost" or host.endswith(".localhost"):
        return "the webhook cannot point at this server (localhost)"
    literal = _literal_address(host)
    if literal is not None and address_is_blocked(literal):
        return "the webhook cannot point at a private or internal address"
    return None


class WebhookRefused(Exception):
    pass


async def resolve_public(host: str, port: int) -> list[str]:
    """Addresses of a host, all of which must be public (one internal record is enough
    to refuse: the connection could land on it)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise WebhookRefused(f"{host} does not resolve")
    for text in addresses:
        if address_is_blocked(ipaddress.ip_address(text.split("%")[0])):
            raise WebhookRefused(f"{host} resolves to a private or internal address")
    return addresses


async def post_webhook(url: str, payload: dict) -> None:
    """POST the payload as JSON. The host is resolved here, checked, and the request
    goes to that address (so a second lookup cannot return a different one); a
    redirect is an error, never followed."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    headers = {}
    extensions = {}
    auth = (unquote(parts.username), unquote(parts.password or "")) if parts.username else None
    target = url
    if not settings.notify_allow_private_webhooks:
        problem = webhook_problem(url)
        if problem:
            raise WebhookRefused(problem)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        address = (await resolve_public(host, port))[0]
        shown = f"[{address}]" if ":" in address else address
        target = parts._replace(netloc=f"{shown}:{port}").geturl()
        headers["Host"] = parts.netloc.rsplit("@", 1)[-1]
        extensions["sni_hostname"] = host
    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = await client.post(target, json=payload, headers=headers, extensions=extensions, auth=auth)
        if response.is_redirect:
            raise WebhookRefused("the webhook answered with a redirect")
        response.raise_for_status()


def job_link(job: Job) -> str:
    return f"{settings.portal_base_url.rstrip('/')}/jobs/{job.id}"


def describe(job: Job, status: str, project: str, failure: dict | None) -> tuple[str, str]:
    """(subject, body) of the e-mail."""
    subject = f"Job {job.entrypoint} {status}"
    lines = [f"Your job {job.entrypoint} in project {project} {status}.", ""]
    if failure:
        lines += [failure["title"], failure["advice"], ""]
    lines.append(f"Details and logs: {job_link(job)}")
    return subject, "\n".join(lines) + "\n"


def webhook_payload(job: Job, status: str, project: str, failure: dict | None) -> dict:
    subject, _body = describe(job, status, project, failure)
    return {
        "event": "job.finished",
        "text": f"{subject}: {job_link(job)}",
        "job_id": str(job.id),
        "project_id": str(job.project_id),
        "project": project,
        "entrypoint": job.entrypoint,
        "status": status,
        "url": job_link(job),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "failure": failure,
    }


class JobNotifier:
    def __init__(self, email: EmailSender | None = None, webhook: WebhookPoster | None = None) -> None:
        self.email = email or send_email
        self.webhook = webhook or post_webhook

    async def _due(self, session: AsyncSession, now: datetime) -> list[Job]:
        return list(
            (
                await session.scalars(
                    select(Job)
                    .where(
                        Job.completed_at.is_not(None),
                        Job.completed_at >= now - RECENT,
                        Job.notified_at.is_(None),
                        Job.notify_tries < settings.notify_max_tries,
                    )
                    .options(selectinload(Job.attempts))
                    .order_by(Job.completed_at.desc())
                    .limit(100)
                )
            ).all()
        )

    async def _announce(self, job: Job, user: User, project: str, failure: dict | None) -> None:
        status = derived_status(job)
        if user.notify_email and email_enabled():
            subject, body = describe(job, status, project, failure)
            await self.email(user.email, subject, body)
        if user.notify_webhook_url:
            await self.webhook(user.notify_webhook_url, webhook_payload(job, status, project, failure))

    async def run_once(self, session: AsyncSession, now: datetime) -> int:
        """One pass; returns how many jobs were announced. The caller commits."""
        sent = 0
        for job in await self._due(session, now):
            user = await session.get(User, job.user_id)
            if user is None or not (user.notify_webhook_url or (user.notify_email and email_enabled())):
                job.notified_at = now  # nobody to tell
                continue
            project = await session.get(Project, job.project_id)
            failure = None
            if derived_status(job) == "failed":
                attempt = latest_attempt(job)
                failure = explain_failure(
                    attempt, await log_tail(attempt), build_failed=attempt is None
                )
            try:
                await self._announce(job, user, project.name if project else "", failure)
            except Exception as exc:  # one bad address must not stop the others
                job.notify_tries += 1
                log.warning("could not notify %s about job %s (try %d): %s", user.email, job.id, job.notify_tries, exc)
                continue
            job.notified_at = now
            sent += 1
        return sent
