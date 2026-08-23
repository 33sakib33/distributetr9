from __future__ import annotations

import argparse
import random
import time
import uuid

import httpx

from harness.client import GPU_CLASSES, FakeAgent
from harness.jobs import Portal


def run(api: str, agents: int, jobs: int, seconds: int, seed: int = 1) -> None:
    rng = random.Random(seed)
    client = httpx.Client(base_url=api, timeout=40.0)
    portal = Portal(client)
    for _ in range(jobs):
        portal.submit_job(min_vram_mb=0)

    fakes = []
    for i in range(agents):
        agent = FakeAgent(
            client,
            hostname=f"sim-{i}-{uuid.uuid4().hex[:6]}",
            caps=GPU_CLASSES[i % len(GPU_CLASSES)],
        )
        agent.register()
        agent.heartbeat()
        fakes.append(agent)

    deadline = time.time() + seconds
    while time.time() < deadline:
        agent = rng.choice(fakes)
        try:
            agent.heartbeat()
            attempt = agent.claim()
            if not attempt:
                continue
            if rng.random() < 0.02:
                continue  # disconnect after claim, no renew
            agent.acknowledge(attempt)
            agent.event(attempt, "running")
            if rng.random() < 0.05:
                portal.cancel(attempt["job_id"])
            if rng.random() < 0.02:
                agent.complete(attempt, fencing_token=str(uuid.uuid4()))
            result = agent.complete(attempt)
            if rng.random() < 0.05:
                agent.complete(attempt)
            if result["status_code"] >= 500:
                agent.complete(attempt)  # lost-response retry
        except httpx.HTTPError:
            continue
    client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake-agent scheduler stress harness")
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--agents", type=int, default=100)
    parser.add_argument("--jobs", type=int, default=500)
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    run(args.api, args.agents, args.jobs, args.seconds)


if __name__ == "__main__":
    main()
