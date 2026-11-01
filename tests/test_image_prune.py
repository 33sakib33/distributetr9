"""Agent image-cache pruning (B3): selection is a pure function; the loop around
it is checked with docker faked out."""
from __future__ import annotations

import subprocess

from agent import executor
from agent.executor import is_lab_repo, prune_images, select_images_to_prune

GB = 1024**3
LAB_ENV = "reg:5000/lab-env/aaaa@sha256:" + "a" * 64
LAB_MIRROR = "reg:5000/lab/python-cpu:3.12.7-slim"
CATALOG = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
FOREIGN = "ubuntu:22.04"


def image(image_id: str, *refs: str) -> dict:
    return {"id": "sha256:" + image_id * 64, "refs": list(refs)}


def ids(images: list[dict]) -> list[str]:
    return [i["id"][7] for i in images]


def test_lab_repositories_are_recognised():
    assert is_lab_repo("reg:5000/lab-env/abc")
    assert is_lab_repo("127.0.0.1:5001/lab/python-cpu")
    assert not is_lab_repo("pytorch/pytorch")
    assert not is_lab_repo("lab-tools/x")


def test_least_recently_used_goes_first():
    images = [image("1", LAB_ENV), image("2", LAB_MIRROR), image("3", CATALOG)]
    last_used = {LAB_ENV: 300.0, LAB_MIRROR: 100.0, CATALOG: 200.0}
    assert ids(select_images_to_prune(images, last_used, set())) == ["2", "3", "1"]


def test_only_lab_and_catalog_images_qualify():
    images = [image("1", LAB_ENV), image("2", FOREIGN), image("3", CATALOG)]
    # CATALOG qualifies because the agent ran it; the owner's ubuntu never does.
    assert ids(select_images_to_prune(images, {CATALOG: 5.0}, set())) == ["1", "3"]
    assert ids(select_images_to_prune(images, {}, set())) == ["1"]


def test_images_of_containers_are_kept():
    images = [image("1", LAB_ENV), image("2", LAB_MIRROR)]
    running = {"sha256:" + "1" * 64}
    assert ids(select_images_to_prune(images, {}, running)) == ["2"]


def test_untracked_lab_image_counts_as_oldest():
    images = [image("1", LAB_ENV), image("2", LAB_MIRROR)]
    assert ids(select_images_to_prune(images, {LAB_ENV: 50.0}, set())) == ["2", "1"]


def test_usage_of_any_tag_protects_the_image():
    both = image("1", LAB_MIRROR, "reg:5000/lab/python-cpu@sha256:" + "b" * 64)
    other = image("2", LAB_ENV)
    last_used = {"reg:5000/lab/python-cpu@sha256:" + "b" * 64: 900.0, LAB_ENV: 10.0}
    assert ids(select_images_to_prune([both, other], last_used, set())) == ["2", "1"]


def test_foreign_tags_on_a_lab_image_are_left_alone():
    shared = image("1", LAB_MIRROR, FOREIGN)
    (picked,) = select_images_to_prune([shared], {}, set())
    assert picked["refs"] == [LAB_MIRROR]


def test_untagged_ref_in_usage_matches_latest():
    images = [image("1", "reg:5000/lab-env/zzz:latest"), image("2", LAB_ENV)]
    last_used = {"reg:5000/lab-env/zzz": 1.0, LAB_ENV: 2.0}
    assert ids(select_images_to_prune(images, last_used, set())) == ["1", "2"]


def _fake_cache(monkeypatch, images: list[dict], free: dict):
    removed: list[str] = []

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["docker", "image", "rm"]
        removed.append(cmd[3])
        free["bytes"] += free["per_image"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(executor, "list_local_images", lambda: images)
    monkeypatch.setattr(executor, "running_image_ids", lambda: set())
    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    return removed


def test_prune_stops_once_free_space_is_above_three_watermarks(monkeypatch):
    images = [image("1", LAB_ENV), image("2", LAB_MIRROR), image("3", CATALOG)]
    free = {"bytes": 1 * GB, "per_image": 1 * GB}  # watermark 1 GB: starts below 2, stops at 3
    removed = _fake_cache(monkeypatch, images, free)
    lines: list[str] = []
    done = prune_images(
        last_used={LAB_MIRROR: 1.0, CATALOG: 2.0, LAB_ENV: 3.0},
        free_bytes=lambda: free["bytes"],
        watermark=1 * GB,
        log=lines.append,
    )
    assert removed == done == [LAB_MIRROR, CATALOG]
    assert len(lines) == 2 and all("removed" in line for line in lines)


def test_prune_does_nothing_with_enough_space(monkeypatch):
    free = {"bytes": 2 * GB, "per_image": GB}
    removed = _fake_cache(monkeypatch, [image("1", LAB_ENV)], free)
    assert prune_images(last_used={}, free_bytes=lambda: free["bytes"], watermark=GB) == []
    assert removed == []


def test_prune_removes_everything_eligible_when_space_never_recovers(monkeypatch):
    free = {"bytes": 0, "per_image": 0}
    removed = _fake_cache(monkeypatch, [image("1", LAB_ENV), image("2", FOREIGN)], free)
    prune_images(last_used={}, free_bytes=lambda: free["bytes"], watermark=GB, log=lambda _l: None)
    assert removed == [LAB_ENV]


def test_heartbeat_carries_the_disk_watermark(client, monkeypatch):
    from harness.client import FakeAgent

    agent = FakeAgent(client, hostname="prune-hb")
    agent.register()
    monkeypatch.setattr("app.routers.agent.settings.disk_watermark_bytes", 123456789)
    assert agent.heartbeat()["disk_watermark_bytes"] == 123456789
