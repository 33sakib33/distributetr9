"""Failure explanations (B25): one case per rule, and the job API."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.failure_explain import explain_failure
from harness.client import FakeAgent
from harness.jobs import Portal


def _attempt(category: str | None = None, exit_code: int | None = 1):
    return SimpleNamespace(failure_category=category, exit_code=exit_code)


@pytest.mark.parametrize(
    ("category", "exit_code", "tail", "title"),
    [
        ("cancelled", 1, "", "Cancelled"),
        ("timeout", 143, "Traceback ...", "Ran out of time"),
        ("lease_lost", None, "", "The lab PC stopped responding"),
        ("node_lost", None, "", "The lab PC stopped responding"),
        ("environment", 1, "", "The environment could not be started"),
        ("user_code", 1, "RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB", "GPU out of memory"),
        ("user_code", 1, "torch.cuda.OutOfMemoryError: CUDA out of memory", "GPU out of memory"),
        ("user_code", 1, "tensorflow.python.framework.errors_impl.ResourceExhaustedError: OOM", "GPU out of memory"),
        ("user_code", 137, "epoch 3 done", "The job was killed"),
        ("user_code", 1, "loading data\nKilled\n", "The job was killed"),
        ("user_code", 1, "ModuleNotFoundError: No module named 'torchvision'", "A Python package is missing"),
        ("user_code", 1, "FileNotFoundError: [Errno 2] No such file or directory: 'data/x.csv'", "A file was not found"),
        ("resource", 1, "", "The outputs were too large"),
        ("user_code", 1, "ValueError: shapes do not match", "Your script stopped with an error"),
    ],
)
def test_each_rule_names_its_problem(category, exit_code, tail, title):
    explained = explain_failure(_attempt(category, exit_code), tail)
    assert explained is not None and explained["title"] == title
    assert explained["advice"]


def test_a_missing_module_names_the_package_to_add():
    explained = explain_failure(_attempt("user_code"), "ModuleNotFoundError: No module named 'cv2.dnn'")
    assert "'cv2'" in explained["advice"] and "opencv-python" in explained["advice"]
    plain = explain_failure(_attempt("user_code"), "ImportError: No module named 'einops'")
    assert "einops" in plain["advice"]


def test_a_missing_file_names_the_path():
    tail = "FileNotFoundError: [Errno 2] No such file or directory: '/home/me/data/train.csv'"
    assert "/home/me/data/train.csv" in explain_failure(_attempt("user_code"), tail)["advice"]


def test_a_build_failure_needs_no_attempt():
    assert explain_failure(None, "", build_failed=True)["title"] == "The environment could not be built"
    assert explain_failure(None, "") is None


def test_a_category_beats_what_the_log_says():
    # killed for time: the traceback of the SIGTERM must not read as a code bug
    assert explain_failure(_attempt("timeout"), "ModuleNotFoundError: No module named 'x'")["title"] == "Ran out of time"


def test_unknown_failures_get_no_explanation():
    assert explain_failure(_attempt("internal", 1), "boom") is None
    assert explain_failure(_attempt(None, None), "") is None


def _fail_job(client, lines: list[str], *, exit_code: int = 1, category: str = "user_code") -> dict:
    tag = f"fx-{uuid.uuid4().hex[:8]}"
    agent = FakeAgent(
        client,
        hostname=f"fx-{tag}",
        caps={"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0},
    )
    agent.register()
    agent.heartbeat()
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    if lines:
        assert agent.post_logs(attempt, lines)["status_code"] == 200
    agent.event(attempt, "uploading")
    agent.complete(attempt, exit_code=exit_code, failure_category=category)
    return portal.get_job(job["id"])


def test_the_job_carries_the_explanation_read_from_its_log(client):
    job = _fail_job(client, ["epoch 1", "Traceback (most recent call last):", "ModuleNotFoundError: No module named 'sklearn'"])
    assert job["status"] == "failed"
    assert job["failure_explanation"]["title"] == "A Python package is missing"
    assert "scikit-learn" in job["failure_explanation"]["advice"]

    oom = _fail_job(client, ["torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 1 GiB"])
    assert oom["failure_explanation"]["title"] == "GPU out of memory"


def test_exit_code_alone_explains_a_job_without_logs(client):
    job = _fail_job(client, [], exit_code=137)
    assert job["failure_explanation"]["title"] == "The job was killed"


def test_only_failed_jobs_have_an_explanation(client):
    portal = Portal(client)
    job = portal.submit_job(gpu_class=f"fx-{uuid.uuid4().hex[:8]}")
    assert portal.get_job(job["id"])["failure_explanation"] is None
