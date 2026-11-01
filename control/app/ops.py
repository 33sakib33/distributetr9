"""Operator commands for the lab server. Run inside the control container:

    docker compose -f deploy/docker-compose.prod.yml exec control python -m app.ops gc --dry-run
    ... python -m app.ops gc --days 30
    ... python -m app.ops status

gc removes what nothing will read again:
  * outputs of attempts that were never accepted (failed / lost / superseded);
    accepted outputs live under jobs/<id>/promoted/ and are kept
  * custom environment images no job has used for --days; the build is marked
    expired and rebuilt on demand if someone submits that environment again
Then run the registry's own garbage collector to reclaim disk (printed below).
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

import httpx
from sqlalchemy import func, select

from app.alerts import check as check_alerts
from app.config import settings
from app.db import SessionLocal
from app.models import Environment, EnvironmentBuild, Job, JobAttempt, Node
from app.scheduler import db_now
from app.storage import storage


async def gc_attempt_outputs(days: int, dry_run: bool) -> int:
    async with SessionLocal() as session:
        cutoff = await db_now(session) - timedelta(days=days)
        rows = (
            await session.execute(
                select(JobAttempt.output_namespace)
                .join(Job, Job.id == JobAttempt.job_id)
                .where(
                    JobAttempt.completed_at < cutoff,
                    (Job.accepted_attempt_id.is_(None)) | (Job.accepted_attempt_id != JobAttempt.id),
                )
            )
        ).all()
    removed = 0
    for (namespace,) in rows:
        keys = storage.list_keys(namespace)
        removed += len(keys)
        if keys and not dry_run:
            storage.delete_prefix(namespace)
    return removed


def _delete_manifest(digest_ref: str) -> bool:
    """DELETE <registry>/v2/<name>/manifests/<digest> for repo/name@sha256:..."""
    repo, digest = digest_ref.split("@", 1)
    host, name = repo.split("/", 1)
    scheme = "http" if host.startswith(("127.", "localhost", "registry:")) else "https"
    try:
        resp = httpx.delete(f"{scheme}://{host}/v2/{name}/manifests/{digest}", timeout=30)
    except httpx.HTTPError:
        return False
    return resp.status_code in (202, 404)


async def gc_environment_images(days: int, dry_run: bool) -> list[str]:
    expired: list[str] = []
    async with SessionLocal() as session:
        cutoff = await db_now(session) - timedelta(days=days)
        builds = (await session.scalars(select(EnvironmentBuild).where(EnvironmentBuild.state == "ready"))).all()
        for build in builds:
            env = await session.scalar(select(Environment).where(Environment.name == f"build-{build.environment_hash}"))
            if env is None or not build.image_digest:
                continue
            last_used = await session.scalar(select(func.max(Job.created_at)).where(Job.environment_id == env.id))
            waiting = await session.scalar(
                select(func.count()).select_from(Job).where(
                    Job.environment_id == env.id, Job.accepted_attempt_id.is_(None), Job.desired_state == "run"
                )
            )
            if waiting or (last_used is not None and last_used > cutoff):
                continue
            expired.append(build.image_digest)
            if dry_run:
                continue
            if _delete_manifest(build.image_digest):
                build.state = "expired"
                build.image_digest = None
                env.cuda_preflight_ok = False
        if not dry_run:
            await session.commit()
    return expired


async def gc(days: int, dry_run: bool) -> tuple[int, list[str]]:
    # One event loop for both: the shared engine's pooled connections belong to it.
    return await gc_attempt_outputs(days, dry_run), await gc_environment_images(days, dry_run)


async def status() -> None:
    async with SessionLocal() as session:
        nodes = (
            await session.scalars(select(Node).where(Node.decommissioned_at.is_(None)).order_by(Node.hostname))
        ).all()
        queued = await session.scalar(
            select(func.count()).select_from(Job).where(Job.desired_state == "run", Job.accepted_attempt_id.is_(None))
        )
        alerts = await check_alerts(session)
    by_state: dict[str, int] = {}
    for node in nodes:
        by_state[node.eligibility] = by_state.get(node.eligibility, 0) + 1
    print("nodes:", ", ".join(f"{k} {v}" for k, v in sorted(by_state.items())) or "none")
    print("queued jobs:", queued)
    print("alerts:" if alerts else "alerts: none")
    for alert in alerts:
        print(f"  [{alert.severity}] {alert.message}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.ops")
    sub = parser.add_subparsers(dest="cmd", required=True)
    gc_cmd = sub.add_parser("gc", help="delete unused attempt outputs and environment images")
    gc_cmd.add_argument("--days", type=int, default=30)
    gc_cmd.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="node states, queue size, current alerts")
    args = parser.parse_args()

    if args.cmd == "status":
        asyncio.run(status())
        return
    objects, images = asyncio.run(gc(args.days, args.dry_run))
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {objects} objects from unaccepted attempts older than {args.days} days")
    print(f"{verb} {len(images)} environment images unused for {args.days} days")
    for ref in images:
        print("  ", ref)
    if images and not args.dry_run:
        print("reclaim registry disk with:")
        print("  docker compose -f deploy/docker-compose.prod.yml exec registry "
              "registry garbage-collect --delete-untagged /etc/docker/registry/config.yml")
    if not settings.registry_url:
        print("(REGISTRY_URL unset: no custom environment images in use)")


if __name__ == "__main__":
    main()
