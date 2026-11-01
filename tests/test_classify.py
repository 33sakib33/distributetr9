from __future__ import annotations

from agent.classify import classify_gpu, infer_compute_capability
from agent.executor import format_docker_error


def test_cpu_when_no_gpu():
    assert classify_gpu(None, 0) == "cpu"
    assert classify_gpu("none", 0) == "cpu"


def test_ada_vram_bands():
    assert classify_gpu("NVIDIA GeForce RTX 4070", 12282) == "ada-12g"
    assert classify_gpu("NVIDIA GeForce RTX 4080", 16376) == "ada-16g"
    assert classify_gpu("NVIDIA GeForce RTX 4090", 24564) == "ada-24g"
    assert classify_gpu("NVIDIA GeForce RTX 4060", 8188) == "ada-8g"


def test_ampere_vram_bands():
    assert classify_gpu("NVIDIA GeForce RTX 3060", 12288) == "ampere-12g"
    assert classify_gpu("NVIDIA GeForce RTX 3080", 10240) == "ampere-12g"
    assert classify_gpu("NVIDIA GeForce RTX 3090", 24576) == "ampere-24g"
    assert classify_gpu("NVIDIA GeForce RTX 3050", 8192) == "ampere-8g"
    assert classify_gpu("NVIDIA GeForce RTX 3050", 6144) == "ampere-8g"
    assert classify_gpu("NVIDIA GeForce RTX 3070", 8192) == "ampere-8g"


def test_turing_vram_bands():
    assert classify_gpu("NVIDIA GeForce GTX 1660", 6144) == "turing-6g"
    assert classify_gpu("NVIDIA GeForce GTX 1660 SUPER", 6144) == "turing-6g"
    assert classify_gpu("NVIDIA GeForce RTX 2060", 6144) == "turing-6g"
    assert classify_gpu("NVIDIA GeForce RTX 2070 SUPER", 8192) == "turing-8g"
    assert classify_gpu("NVIDIA GeForce RTX 2080 Ti", 11264) == "turing-11g"


def test_old_cards_unsupported():
    assert classify_gpu("NVIDIA GeForce GTX 1080 Ti", 11264) == "unsupported"
    assert classify_gpu("NVIDIA GeForce GTX 1650", 4096) == "unsupported"


def test_compute_capability_hints():
    assert infer_compute_capability("NVIDIA GeForce RTX 4070") == "8.9"
    assert infer_compute_capability("NVIDIA GeForce RTX 3080") == "8.6"
    assert infer_compute_capability("NVIDIA GeForce RTX 3050") == "8.6"
    assert infer_compute_capability("NVIDIA GeForce GTX 1660") == "7.5"


def test_docker_permission_hint():
    msg = format_docker_error("permission denied while trying to connect to the Docker daemon socket")
    assert "usermod" in msg
    assert "sg docker" in msg
    assert format_docker_error("docker binary missing") == "docker binary missing"


def test_fake_gpu_overrides_probe(monkeypatch, tmp_path):
    from agent import classify

    monkeypatch.setenv("LAB_FAKE_GPU", "ampere-8g:8192:8.6")
    fields = classify.agent_register_fields(python_bin="python3")
    assert fields["compatibility_class"] == "ampere-8g"
    assert fields["vram_allocatable_mb"] == 8192
    assert fields["compute_capability"] == "8.6"
    assert fields["agent_healthy"] is True

    busy = tmp_path / "busy"
    monkeypatch.setenv("LAB_FAKE_BUSY_FILE", str(busy))
    assert classify.gpu_desktop_busy() is False
    busy.touch()
    assert classify.gpu_desktop_busy() is True
