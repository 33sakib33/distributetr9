from __future__ import annotations

import argparse
import math
import random
import threading
import time
import uuid

import httpx

from harness.client import GPU_CLASSES, FakeAgent
from harness.jobs import Portal

# POST /jobs is limited per user; a lab's jobs come from many people, so the
# harness submits as many users and each stays well under the limit.
JOBS_PER_USER = 20


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for no samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


class Stats:
    """What the fake agents measured. One lock; the loops are I/O bound."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.claim_ms: list[float] = []  # claims that returned a job
        self.empty_claims = 0
        self.completed = 0
        self.errors: list[str] = []
        self.first_at: float | None = None
        self.last_completed_at: float | None = None

    def claimed(self, ms: float) -> None:
        with self._lock:
            self.claim_ms.append(ms)

    def empty(self) -> None:
        with self._lock:
            self.empty_claims += 1

    def finished(self) -> None:
        with self._lock:
            self.completed += 1
            self.last_completed_at = time.monotonic()

    def error(self, what: str) -> None:
        with self._lock:
            self.errors.append(what)

    def summary(self, started_at: float, ended_at: float) -> dict:
        busy_until = self.last_completed_at or ended_at
        elapsed = max(busy_until - started_at, 1e-9)
        return {
            "claims_with_job": len(self.claim_ms),
            "empty_claims": self.empty_claims,
            "claim_p50_ms": round(percentile(self.claim_ms, 50), 1),
            "claim_p95_ms": round(percentile(self.claim_ms, 95), 1),
            "claim_max_ms": round(max(self.claim_ms, default=0.0), 1),
            "completed_jobs": self.completed,
            "jobs_per_second": round(self.completed / elapsed, 2),
            "busy_seconds": round(elapsed, 1),
            "errors": len(self.errors),
        }


def format_summary(summary: dict) -> str:
    return "\n".join(f"{key:>18}: {value}" for key, value in summary.items())


def submit_jobs(api: str, jobs: int, stats: Stats) -> None:
    """Queue the jobs as several users, with a short wait when throttled."""
    client = httpx.Client(base_url=api, timeout=60.0)
    try:
        portals = [Portal(client) for _ in range(math.ceil(jobs / JOBS_PER_USER))]
        for number in range(jobs):
            portal = portals[number // JOBS_PER_USER]
            for _attempt in range(5):
                try:
                    portal.submit_job(min_vram_mb=0, project_id=portal.project_id())
                    break
                except AssertionError as exc:
                    if "rate_limited" not in str(exc):
                        stats.error(f"submit: {exc}"[:200])
                        break
                    time.sleep(2)
            else:
                stats.error("submit: still throttled")
    finally:
        client.close()


def agent_loop(
    api: str,
    index: int,
    deadline: float,
    job_seconds: float,
    stats: Stats,
    rng: random.Random,
    chaos: bool,
    jobs: int,
) -> None:
    client = httpx.Client(base_url=api, timeout=60.0)
    agent = FakeAgent(
        client,
        hostname=f"sim-{index}-{uuid.uuid4().hex[:6]}",
        caps=GPU_CLASSES[index % len(GPU_CLASSES)],
    )
    try:
        agent.register()
        agent.heartbeat()
        while time.monotonic() < deadline and not (not chaos and stats.completed >= jobs):
            try:
                agent.heartbeat()
                begun = time.perf_counter()
                attempt = agent.claim()
                took = (time.perf_counter() - begun) * 1000
                if not attempt:
                    stats.empty()
                    continue
                stats.claimed(took)
                _work(agent, attempt, job_seconds, stats, rng, chaos)
            except (httpx.HTTPError, AssertionError) as exc:
                stats.error(f"agent {index}: {type(exc).__name__}: {exc}"[:200])
                time.sleep(0.5)
    finally:
        client.close()


def _expect(stats: Stats, what: str, result: dict) -> bool:
    if result["status_code"] >= 400:
        stats.error(f"{what}: {result['status_code']} {result['body']}"[:200])
        return False
    return True


def _work(agent: FakeAgent, attempt: dict, job_seconds: float, stats: Stats, rng: random.Random, chaos: bool) -> None:
    """Run one fake job: acknowledge, run (renewing the lease), upload, complete."""
    if chaos and rng.random() < 0.02:
        return  # the PC vanishes after the claim; the lease worker takes the job back
    if not _expect(stats, "acknowledge", agent.acknowledge(attempt)):
        return
    if not _expect(stats, "running", agent.event(attempt, "running")):
        return
    # Real jobs differ in length; identical ones would send every PC back for work at the same instant.
    end = time.monotonic() + job_seconds * rng.uniform(0.5, 1.5)
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(left, 3.0))
        if not _expect(stats, "renew", agent.renew(attempt)):
            return
    agent.post_logs(attempt, ["epoch 1", "epoch 2"])
    if chaos and rng.random() < 0.02:
        agent.complete(attempt, fencing_token=str(uuid.uuid4()))  # stale token: refused, on purpose
    if not _expect(stats, "uploading", agent.event(attempt, "uploading")):
        return
    result = agent.complete(attempt)
    if _expect(stats, "complete", result):
        stats.finished()


def run(
    api: str,
    agents: int,
    jobs: int,
    seconds: int,
    seed: int = 1,
    job_seconds: float = 10.0,
    chaos: bool = False,
) -> dict:
    stats = Stats()
    submit_jobs(api, jobs, stats)
    started = time.monotonic()
    deadline = started + seconds
    threads = [
        threading.Thread(
            target=agent_loop,
            args=(api, i, deadline, job_seconds, stats, random.Random(seed + i), chaos, jobs),
            daemon=True,
        )
        for i in range(agents)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(1.0, deadline - time.monotonic() + 90))
    return stats.summary(started, time.monotonic())


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake-agent load test: claim latency, throughput and errors")
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--agents", type=int, default=100)
    parser.add_argument("--jobs", type=int, default=500)
    parser.add_argument("--seconds", type=int, default=60, help="longest the agents keep working")
    parser.add_argument("--job-seconds", type=float, default=10.0, help="how long each fake job runs")
    parser.add_argument("--chaos", action="store_true", help="also drop leases and send stale fencing tokens")
    args = parser.parse_args()
    summary = run(args.api, args.agents, args.jobs, args.seconds, job_seconds=args.job_seconds, chaos=args.chaos)
    print(format_summary(summary))
    raise SystemExit(1 if summary["errors"] else 0)


if __name__ == "__main__":
    main()
