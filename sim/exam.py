"""Failure exam for the simulated lab: a scripted rehearsal of the Phase 12
five-machine exam. Each scenario breaks something with `docker` and checks the
outcome through the control API.

    make sim            # start the simulated lab
    make sim-exam       # run every scenario
    control/.venv/bin/python -m sim.exam kill_agent reboot   # run some
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable

import httpx

API = "http://127.0.0.1:8090"
USER = {"X-Dev-User": "exam@uni.edu"}
ADMIN = {"X-Dev-User": "admin@uni.edu"}
NETWORK = "distributetr9_default"
TRAIN_NODES = ("sim-pc-1", "sim-pc-2", "sim-pc-3", "sim-pc-4")
ALL_NODES = TRAIN_NODES + ("sim-builder",)

QUICK = """
import os, socket
out = os.environ.get("OUTPUT_DIR", "output")
os.makedirs(out, exist_ok=True)
print("hello from", socket.gethostname())
open(os.path.join(out, "out.txt"), "w").write("ok")
"""


def sleeper(seconds: int) -> str:
    return f"""
import os, time
print("working", flush=True)
time.sleep({seconds})
out = os.environ.get("OUTPUT_DIR", "output")
os.makedirs(out, exist_ok=True)
open(os.path.join(out, "out.txt"), "w").write("done")
"""


def sh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def node_sh(node: str, cmd: str, check: bool = True) -> str:
    return sh("docker", "exec", node, "sh", "-c", cmd, check=check)


def wait_for(what: str, fn: Callable[[], object], timeout: float, every: float = 1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what} (last: {last})")


class Lab:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=API, timeout=60)
        projects = self.get("/projects")["projects"]
        if projects:
            self.project_id = projects[0]["id"]
        else:
            self.project_id = self.post("/projects", json={"name": "exam"})["id"]
        envs = {e["name"]: e["id"] for e in self.get("/environments")["environments"]}
        # The slim-Python catalog image keeps simulated jobs small and multi-arch.
        self.env_id = envs["tensorflow-stable"]

    def get(self, path: str, headers: dict = USER) -> dict:
        resp = self.http.get(path, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, headers: dict = USER, **kw) -> dict:
        resp = self.http.post(path, headers=headers, **kw)
        if resp.status_code >= 400:
            raise AssertionError(f"POST {path} -> {resp.status_code} {resp.text}")
        return resp.json()

    def submit(
        self,
        source: str,
        *,
        gpu_class: str | None = None,
        min_vram_mb: int = 0,
        runtime: int = 300,
        requirements: str | None = None,
        key: str | None = None,
    ) -> str:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = source.encode()
            info = tarfile.TarInfo("train.py")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        form = {
            "project_id": self.project_id,
            "entrypoint": "train.py",
            "environment_id": self.env_id,
            "args": "[]",
            "resources": json.dumps(
                {"gpu_class": gpu_class, "min_vram_mb": min_vram_mb, "max_runtime_seconds": runtime}
            ),
        }
        if requirements:
            form["requirements"] = requirements
        headers = dict(USER, **({"Idempotency-Key": key} if key else {}))
        files = {"archive": ("job.tar.gz", buf.getvalue(), "application/gzip")}
        return self.post("/jobs", headers=headers, data=form, files=files)["id"]

    def job(self, job_id: str) -> dict:
        return self.get(f"/jobs/{job_id}")

    def wait_status(self, job_id: str, statuses: set[str], timeout: float) -> dict:
        def check():
            job = self.job(job_id)
            if job["status"] in statuses:
                return job
            if job["status"] in {"completed", "failed", "cancelled"}:
                raise AssertionError(f"job ended {job['status']}, wanted {statuses}: {job.get('latest_attempt')}")
            return None

        return wait_for(f"job {job_id[:8]} to reach {statuses}", check, timeout)

    def nodes(self) -> dict[str, dict]:
        return {n["hostname"]: n for n in self.get("/nodes", headers=ADMIN)["nodes"]}

    def outputs(self, job_id: str) -> list[dict]:
        return [a for a in self.get(f"/jobs/{job_id}/artifacts")["artifacts"] if a["type"] == "output"]


# --- scenarios -------------------------------------------------------------


def lab_up(lab: Lab) -> str:
    def ready():
        nodes = lab.nodes()
        missing = [n for n in ALL_NODES if nodes.get(n, {}).get("eligibility") != "eligible"]
        return not missing and nodes

    wait_for("all five simulated PCs to register", ready, 180, every=2)
    return "4 train nodes + 1 builder eligible"


def self_update(lab: Lab) -> str:
    target = lab.get("/nodes", headers=ADMIN)["agent_target_version"]
    wait_for(
        f"sim-pc-1 to update 0.0.1 -> {target}",
        lambda: lab.nodes()["sim-pc-1"]["agent_version"] == target,
        120,
        every=2,
    )
    current = node_sh("sim-pc-1", "readlink /opt/lab-agent/current")
    assert current == f"releases/{target}", current
    wait_for(
        "the new release to confirm itself",
        lambda: subprocess.run(["docker", "exec", "sim-pc-1", "test", "!", "-e", "/opt/lab-agent/pending"]).returncode == 0,
        30,
    )
    return f"sim-pc-1 updated itself to {target} and confirmed the release"


def happy_path(lab: Lab) -> str:
    ids = [lab.submit(QUICK) for _ in range(6)]
    hosts = set()
    for job_id in ids:
        job = lab.wait_status(job_id, {"completed"}, 240)
        hosts.add(job["latest_attempt"]["hostname"])
        assert len(lab.outputs(job_id)) == 1, f"job {job_id} promoted {len(lab.outputs(job_id))} outputs"
    return f"6 jobs completed on {sorted(hosts)}, one promoted output each"


def gpu_routing(lab: Lab) -> str:
    big = lab.submit(QUICK, gpu_class="ada-24g", min_vram_mb=20000)
    small = lab.submit(QUICK, gpu_class="ampere-8g", min_vram_mb=6000)
    big_host = lab.wait_status(big, {"completed"}, 120)["latest_attempt"]["hostname"]
    small_host = lab.wait_status(small, {"completed"}, 120)["latest_attempt"]["hostname"]
    assert big_host == "sim-pc-4", big_host
    assert small_host in {"sim-pc-1", "sim-pc-2"}, small_host
    return f"24 GB job ran on {big_host}, 8 GB job on {small_host}"


def custom_env_via_registry(lab: Lab) -> str:
    source = "import six\nprint('six', six.__version__)\n" + QUICK
    job_id = lab.submit(source, requirements="six==1.16.0\n")
    job = lab.wait_status(job_id, {"completed"}, 300)
    host = job["latest_attempt"]["hostname"]
    assert host != "sim-builder"
    images = node_sh(host, "docker images --digests --format '{{.Repository}}@{{.Digest}}'")
    pulled = [line for line in images.splitlines() if line.startswith("registry:5000/lab-env/")]
    assert pulled, f"{host} has no lab-env image from the registry: {images}"
    return f"built on sim-builder, pulled by digest and run on {host}"


def _running_job(lab: Lab, seconds: int, **kw) -> tuple[str, str]:
    job_id = lab.submit(sleeper(seconds), **kw)
    job = lab.wait_status(job_id, {"running"}, 120)
    return job_id, job["latest_attempt"]["hostname"]


def _completed_after_loss(lab: Lab, job_id: str, timeout: float = 150) -> dict:
    job = lab.wait_status(job_id, {"completed"}, timeout)
    attempt = job["latest_attempt"]
    assert attempt["attempt_number"] >= 2, f"expected a retry, got attempt {attempt['attempt_number']}"
    assert len(lab.outputs(job_id)) == 1, "a lost attempt's output was promoted"
    return job


def kill_agent(lab: Lab) -> str:
    job_id, host = _running_job(lab, 20)
    node_sh(host, "pkill -f agent.py")
    job = _completed_after_loss(lab, job_id)
    leftovers = node_sh(host, "docker ps -q --filter name=lab-")
    assert not leftovers or job["latest_attempt"]["hostname"] == host, f"orphan container left on {host}"
    return f"agent on {host} killed mid-job; retried as attempt {job['latest_attempt']['attempt_number']} on {job['latest_attempt']['hostname']}"


def reboot(lab: Lab) -> str:
    job_id, host = _running_job(lab, 20)
    sh("docker", "restart", "-t", "1", host)
    job = _completed_after_loss(lab, job_id, timeout=180)
    wait_for(f"{host} to come back", lambda: lab.nodes()[host]["eligibility"] == "eligible", 90, every=2)
    return f"{host} rebooted mid-job; job finished on {job['latest_attempt']['hostname']}, {host} back online"


def network_drop(lab: Lab) -> str:
    job_id, host = _running_job(lab, 30)
    sh("docker", "network", "disconnect", NETWORK, host)
    try:
        job = _completed_after_loss(lab, job_id, timeout=180)
    finally:
        sh("docker", "network", "connect", NETWORK, host, check=False)
    wait_for(f"{host} to rejoin", lambda: lab.nodes()[host]["eligibility"] == "eligible", 120, every=2)
    # The cut-off node finishes its stale attempt later; it must stay fenced.
    time.sleep(15)
    again = lab.job(job_id)
    assert again["accepted_attempt_id"] == job["accepted_attempt_id"], "accepted attempt changed after rejoin"
    assert len(lab.outputs(job_id)) == 1
    return f"{host} cut off for the lease; job retried on {job['latest_attempt']['hostname']}, stale result fenced"


def cancel_running(lab: Lab) -> str:
    job_id, host = _running_job(lab, 120)
    lab.post(f"/jobs/{job_id}/cancel")
    lab.wait_status(job_id, {"cancelled"}, 60)
    wait_for(f"the container on {host} to be removed", lambda: node_sh(host, "docker ps -q --filter name=lab-") == "", 30)
    return f"running job on {host} cancelled, container removed"


def disk_full(lab: Lab) -> str:
    node_sh("sim-pc-3", "echo 1000 > /sim/disk_free")
    try:
        time.sleep(5)
        job_id = lab.submit(QUICK, gpu_class="ada-12g", min_vram_mb=4000)
        time.sleep(12)
        job = lab.job(job_id)
        assert job["status"] == "queued", job["status"]
        assert job["pending_reason"] == "waiting for disk on eligible nodes", job["pending_reason"]
    finally:
        node_sh("sim-pc-3", "rm -f /sim/disk_free")
    lab.wait_status(job_id, {"completed"}, 90)
    return "sim-pc-3 refused work while its disk was full, ran it once space returned"


def desktop_busy(lab: Lab) -> str:
    node_sh("sim-pc-3", "touch /sim/busy")
    try:
        time.sleep(5)
        job_id = lab.submit(QUICK, gpu_class="ada-12g", min_vram_mb=4000)
        time.sleep(10)
        assert lab.job(job_id)["status"] == "queued"
    finally:
        node_sh("sim-pc-3", "rm -f /sim/busy")
    lab.wait_status(job_id, {"completed"}, 90)
    return "sim-pc-3 held work while its desktop GPU was busy"


def max_runtime(lab: Lab) -> str:
    job_id = lab.submit(sleeper(120), runtime=5)
    job = lab.wait_status(job_id, {"failed"}, 90)
    category = job["latest_attempt"]["failure_category"]
    assert category == "timeout", category
    return "job over its 5 s limit was stopped and marked timeout"


def duplicate_submit(lab: Lab) -> str:
    key = f"exam-{uuid.uuid4().hex}"
    first = lab.submit(QUICK, key=key)
    second = lab.submit(QUICK, key=key)
    assert first == second, (first, second)
    lab.wait_status(first, {"completed"}, 120)
    return "two submits with one Idempotency-Key made one job"


SCENARIOS: list[Callable[[Lab], str]] = [
    lab_up,
    self_update,
    happy_path,
    gpu_routing,
    custom_env_via_registry,
    kill_agent,
    reboot,
    network_drop,
    cancel_running,
    disk_full,
    desktop_busy,
    max_runtime,
    duplicate_submit,
]


def main(argv: list[str]) -> int:
    wanted = set(argv)
    lab = Lab()
    results = []
    for scenario in SCENARIOS:
        if wanted and scenario.__name__ not in wanted and scenario is not lab_up:
            continue
        start = time.time()
        try:
            detail = scenario(lab)
            ok = True
        except Exception as exc:  # report and keep going
            detail = f"{type(exc).__name__}: {exc}"
            ok = False
        took = time.time() - start
        results.append((scenario.__name__, ok, took, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {scenario.__name__:<24} {took:6.1f}s  {detail}", flush=True)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
