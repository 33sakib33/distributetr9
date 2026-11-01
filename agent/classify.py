"""GPU class mapping, host probe, and desktop-GPU busy heuristic. Stdlib only."""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

INTERACTIVE_PROCS = (
    "steam.exe",
    "steamwebhelper",
    "epicgameslauncher",
    "discord",
    "geforcenow",
    "nvidia share",
    "gameoverlayui",
    "origin.exe",
    "battle.net",
)

_FALLBACK_CLASSES = {
    "arch_markers": {
        "ada": ["4090", "4080", "4070", "4060", "4050", "rtx 2000 ada", "rtx 4000 ada"],
        "ampere": ["3090", "3080", "3070", "3060", "3050", "a2000", "a4000", "a5000", "a6000"],
    },
    "classes": [
        {"name": "ada-24g", "arch": "ada", "min_vram_mb": 20480, "max_vram_mb": 999999},
        {"name": "ada-16g", "arch": "ada", "min_vram_mb": 14336, "max_vram_mb": 20479},
        {"name": "ada-12g", "arch": "ada", "min_vram_mb": 10240, "max_vram_mb": 14335},
        {"name": "ada-8g", "arch": "ada", "min_vram_mb": 5120, "max_vram_mb": 10239},
        {"name": "ampere-24g", "arch": "ampere", "min_vram_mb": 20480, "max_vram_mb": 999999},
        {"name": "ampere-12g", "arch": "ampere", "min_vram_mb": 10240, "max_vram_mb": 20479},
        {"name": "ampere-8g", "arch": "ampere", "min_vram_mb": 5120, "max_vram_mb": 10239},
    ],
}


def load_catalog() -> dict:
    candidates = [
        Path(__file__).resolve().parent.parent / "inventory" / "classes.json",
        Path(__file__).resolve().parent / "classes.json",
        Path("/etc/lab-agent/classes.json"),
    ]
    for path in candidates:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return _FALLBACK_CLASSES


def _arch_for(gpu_model: str, catalog: dict) -> str | None:
    lowered = gpu_model.lower()
    for arch, markers in (catalog.get("arch_markers") or {}).items():
        for marker in markers:
            if marker.lower() in lowered:
                return arch
    return None


def classify_gpu(gpu_model: str | None, vram_mb: int) -> str:
    if not gpu_model or gpu_model.lower() in {"none", "unknown", ""}:
        return "cpu"
    catalog = load_catalog()
    arch = _arch_for(gpu_model, catalog)
    if not arch:
        return "unsupported"
    for row in catalog.get("classes") or []:
        if row.get("arch") != arch:
            continue
        if int(row["min_vram_mb"]) <= int(vram_mb) <= int(row["max_vram_mb"]):
            return str(row["name"])
    return "unsupported"


def _run(cmd: list[str], timeout: int = 8) -> str | None:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def nvidia_query() -> dict | None:
    line = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version,compute_cap",
            "--format=csv,noheader,nounits",
        ]
    )
    if line is None:
        line = _run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ]
        )
    if not line:
        return None
    first = line.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    name = parts[0]
    try:
        vram = int(float(parts[1])) if len(parts) > 1 else 0
    except ValueError:
        vram = 0
    driver = parts[2] if len(parts) > 2 else "unknown"
    compute = parts[3] if len(parts) > 3 and parts[3] else infer_compute_capability(name)
    return {
        "gpu_model": name,
        "vram_total_mb": vram,
        "vram_allocatable_mb": max(0, vram - 256),
        "driver_version": driver,
        "compute_capability": compute,
    }


# --- Telemetry samples (job usage, node health) -------------------------------
# Each tool call has a short timeout and a missing tool yields None fields:
# sampling must never raise and never hold up a lease renewal.

SAMPLE_TIMEOUT = 5


def _float(raw: str | None) -> float | None:
    try:
        return float((raw or "").strip().rstrip("%"))
    except ValueError:
        return None


def parse_nvidia_usage(output: str | None) -> dict:
    """Parse `utilization.gpu,memory.used,temperature.gpu` CSV lines (one per GPU):
    busiest GPU's utilisation and temperature, memory summed over GPUs."""
    pct: list[float] = []
    vram: list[float] = []
    temp: list[float] = []
    for line in (output or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        for bucket, raw in zip((pct, vram, temp), parts):
            value = _float(raw)
            if value is not None:
                bucket.append(value)
    return {
        "gpu_pct": max(pct) if pct else None,
        "vram_mb": sum(vram) if vram else None,
        "gpu_temp_c": max(temp) if temp else None,
    }


def sample_gpu() -> dict:
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=SAMPLE_TIMEOUT,
    )
    return parse_nvidia_usage(out)


_MEM_UNITS = {
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000**2,
    "mib": 1024**2,
    "gb": 1000**3,
    "gib": 1024**3,
}


def parse_mem_mb(raw: str | None) -> float | None:
    """`12.5MiB / 1GiB` (docker stats MemUsage) -> megabytes used."""
    used = (raw or "").split("/")[0].strip().lower()
    digits = used.rstrip("abcdefghijklmnopqrstuvwxyz")
    unit = used[len(digits) :] or "b"
    value = _float(digits)
    if value is None or unit not in _MEM_UNITS:
        return None
    return value * _MEM_UNITS[unit] / (1024**2)


def parse_docker_stats(output: str | None) -> dict:
    """Parse one `docker stats --format '{{json .}}'` line."""
    try:
        row = json.loads((output or "").strip().splitlines()[0])
    except (IndexError, ValueError):
        return {"cpu_pct": None, "ram_mb": None}
    if not isinstance(row, dict):
        return {"cpu_pct": None, "ram_mb": None}
    return {"cpu_pct": _float(row.get("CPUPerc")), "ram_mb": parse_mem_mb(row.get("MemUsage"))}


def sample_container(name: str | None) -> dict:
    if not name:
        return {"cpu_pct": None, "ram_mb": None}
    out = _run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", name],
        timeout=SAMPLE_TIMEOUT,
    )
    return parse_docker_stats(out)


def collect_usage(container: str | None) -> dict:
    """gpu_pct, vram_mb, cpu_pct, ram_mb for the renew body; None where unknown."""
    try:
        gpu = sample_gpu()
        box = sample_container(container)
    except Exception:
        return {"gpu_pct": None, "vram_mb": None, "cpu_pct": None, "ram_mb": None}
    return {
        "gpu_pct": gpu["gpu_pct"],
        "vram_mb": gpu["vram_mb"],
        "cpu_pct": box["cpu_pct"],
        "ram_mb": box["ram_mb"],
    }


def uptime_seconds(path: str = "/proc/uptime") -> int | None:
    try:
        return int(float(Path(path).read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def docker_ok() -> bool:
    return _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=SAMPLE_TIMEOUT) is not None


def collect_health(*, check_docker: bool) -> dict:
    """Node health for the heartbeat. `docker_ok` is None when docker is not used."""
    try:
        gpu = sample_gpu()
        driver = _run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader,nounits"],
            timeout=SAMPLE_TIMEOUT,
        )
        return {
            "gpu_temp_c": gpu["gpu_temp_c"],
            "gpu_pct": gpu["gpu_pct"],
            "vram_used_mb": None if gpu["vram_mb"] is None else int(gpu["vram_mb"]),
            "driver_version": driver.splitlines()[0].strip() if driver else None,
            "uptime_s": uptime_seconds(),
            "docker_ok": docker_ok() if check_docker else None,
        }
    except Exception:
        return {}


def infer_compute_capability(gpu_model: str) -> str:
    lowered = gpu_model.lower()
    if any(tok in lowered for tok in ("4090", "4080", "4070", "4060", "4050")):
        return "8.9"
    if any(tok in lowered for tok in ("3090", "3080", "3070", "3060", "3050", "a40", "a5000", "a6000")):
        return "8.6"
    if "a4000" in lowered or "a2000" in lowered:
        return "8.6"
    if any(tok in lowered for tok in ("1660", "1650", "2060", "2070", "2080", "titan rtx", "quadro rtx", "t1000")):
        return "7.5"
    return "0.0"


def ram_mb() -> int:
    if sys.platform == "darwin":
        raw = _run(["sysctl", "-n", "hw.memsize"])
        if raw:
            try:
                return max(1, int(raw) // (1024 * 1024))
            except ValueError:
                pass
    if os.path.exists("/proc/meminfo"):
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return max(1, int(line.split()[1]) // 1024)
        except (OSError, ValueError):
            pass
    if os.name == "nt":
        raw = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
            ]
        )
        if raw:
            try:
                return max(1, int(raw.strip().splitlines()[-1]) // (1024 * 1024))
            except ValueError:
                pass
    return 8192


def disk_free_bytes(*targets: str) -> int:
    # Simulated nodes: a file holding the free-byte count stands in for a full disk.
    fake = os.environ.get("LAB_FAKE_DISK_FREE_FILE")
    if fake and Path(fake).exists():
        try:
            return int(Path(fake).read_text().strip())
        except ValueError:
            pass
    paths = list(targets) or [str(Path.cwd()), "/"]
    if os.name == "nt":
        paths.append("C:\\")
    best = 0
    for path in paths:
        try:
            best = max(best, shutil.disk_usage(path).free)
        except OSError:
            continue
    return best


def wsl_info() -> dict:
    distro = os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")
    kernel = platform.release()
    wsl_version = "none"
    if distro:
        wsl_version = os.environ.get("WSL_DISTRO_NAME") or "wsl"
    elif "microsoft" in kernel.lower() or "wsl" in kernel.lower():
        wsl_version = "wsl2"
    status = _run(["wsl.exe", "--status"]) or _run(["wsl", "--status"])
    if status and wsl_version == "none":
        wsl_version = "installed"
    return {
        "wsl_version": wsl_version,
        "kernel": kernel,
        "wsl_present": wsl_version != "none",
    }


def fake_gpu() -> dict | None:
    """LAB_FAKE_GPU=class:vram_mb[:cc] makes a GPU-less machine report a GPU.

    For simulated lab nodes and scheduler testing only. Jobs still run on CPU,
    because the executor adds --gpus only when nvidia-smi works.
    """
    raw = os.environ.get("LAB_FAKE_GPU", "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) < 2:
        raise SystemExit("LAB_FAKE_GPU must be class:vram_mb[:compute_capability]")
    vram = int(parts[1])
    return {
        "compatibility_class": parts[0],
        "gpu_model": "fake " + parts[0],
        "vram_total_mb": vram,
        "vram_allocatable_mb": vram,
        "compute_capability": parts[2] if len(parts) > 2 else "8.6",
        "driver_version": "fake",
    }


def gpu_desktop_busy(*, has_lab_job: bool = False) -> bool:
    if has_lab_job:
        return False
    busy_file = os.environ.get("LAB_FAKE_BUSY_FILE")
    if busy_file:
        return Path(busy_file).exists()
    util_raw = _run(
        ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"]
    )
    util = 0
    if util_raw:
        try:
            util = int(float(util_raw.splitlines()[0].strip()))
        except ValueError:
            util = 0
    procs = _run(
        ["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"]
    ) or ""
    interactive = False
    for line in procs.splitlines():
        lowered = line.lower()
        if "lab-" in lowered:
            continue
        if any(name in lowered for name in INTERACTIVE_PROCS):
            interactive = True
            break
    return util >= 20 or interactive


def cuda_preflight(python_bin: str) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(
            [
                python_bin,
                "-c",
                "import torch; assert torch.cuda.is_available(); torch.zeros(1, device='cuda')",
            ],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, None
    err = (result.stderr or result.stdout or "cuda preflight failed").strip()
    return False, err[:500]


def probe_machine(*, python_bin: str | None = None, cuda_test: bool = False) -> dict:
    python_bin = python_bin or sys.executable
    gpu = nvidia_query()
    wsl = wsl_info()
    if gpu is None:
        record = {
            "gpu_model": "none",
            "vram_total_mb": 0,
            "vram_allocatable_mb": 0,
            "driver_version": "none",
            "compute_capability": "0.0",
            "compatibility_class": "cpu",
        }
        pytorch_ok = None
        pytorch_error = None
    else:
        record = dict(gpu)
        record["compatibility_class"] = classify_gpu(gpu["gpu_model"], gpu["vram_total_mb"])
        pytorch_ok = None
        pytorch_error = None
        if cuda_test:
            pytorch_ok, pytorch_error = cuda_preflight(python_bin)

    recommended = "eligible"
    if record["compatibility_class"] == "unsupported":
        recommended = "quarantined"
    if cuda_test and record["compatibility_class"] != "cpu" and pytorch_ok is False:
        recommended = "quarantined"
    if platform.system() == "Windows" and not wsl["wsl_present"]:
        recommended = "quarantined"

    return {
        "hostname": socket.gethostname(),
        **record,
        "windows_version": platform.platform(),
        "cpu_cores": os.cpu_count() or 1,
        "ram_mb": ram_mb(),
        "disk_windows_free_bytes": disk_free_bytes("C:\\") if os.name == "nt" else 0,
        "disk_wsl_free_bytes": disk_free_bytes("/", str(Path.home())),
        "wsl_version": wsl["wsl_version"],
        "kernel": wsl["kernel"],
        "pytorch_cuda_ok": pytorch_ok,
        "pytorch_error": pytorch_error,
        "gpu_desktop_busy": gpu_desktop_busy(),
        "recommended_eligibility": recommended,
    }


def agent_register_fields(*, python_bin: str, cuda_test: bool = False) -> dict:
    fake = fake_gpu()
    probed = probe_machine(python_bin=python_bin, cuda_test=cuda_test and fake is None)
    if fake:
        probed.update(fake)
        probed["pytorch_cuda_ok"] = True
    healthy = True
    if probed["compatibility_class"] not in {"cpu", "unsupported"} and cuda_test:
        healthy = bool(probed["pytorch_cuda_ok"])
    if probed["compatibility_class"] == "unsupported":
        healthy = False
    return {
        "gpu_model": probed["gpu_model"],
        "vram_total_mb": probed["vram_total_mb"],
        "vram_allocatable_mb": probed["vram_allocatable_mb"],
        "driver_version": probed["driver_version"],
        "compute_capability": probed["compute_capability"],
        "compatibility_class": probed["compatibility_class"],
        "cuda_preflight_ok": healthy,
        "cpu_cores": probed["cpu_cores"],
        "ram_mb": probed["ram_mb"],
        "disk_free_bytes": max(probed["disk_wsl_free_bytes"], probed["disk_windows_free_bytes"]),
        "windows_version": probed["windows_version"],
        "wsl_version": probed["wsl_version"],
        "agent_healthy": healthy,
        "gpu_desktop_busy": probed["gpu_desktop_busy"],
    }
