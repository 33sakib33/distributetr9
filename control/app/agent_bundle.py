"""Signed agent bundles for self-update.

The control plane packs the `agent/` directory into a deterministic tar.gz and
signs it with an RSA key. Agents pin the public key at install time and refuse
any bundle whose signature does not verify, so updates are safe over plain HTTP.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSION_RE = re.compile(r'^AGENT_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
_BUNDLE_FILES = ("*.py", "*.json", "*.sh")


@dataclass(frozen=True)
class Bundle:
    version: str
    data: bytes
    sha256: str
    signature: str


def agent_dir() -> Path:
    return Path(settings.agent_dir) if settings.agent_dir else REPO_ROOT / "agent"


def install_root() -> Path:
    return Path(settings.install_root) if settings.install_root else REPO_ROOT


def _key_path() -> Path:
    return Path(settings.agent_signing_key_path) if settings.agent_signing_key_path else REPO_ROOT / ".dev" / "agent-signing-key.pem"


@lru_cache(maxsize=1)
def signing_key() -> rsa.RSAPrivateKey:
    path = _key_path()
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return key


def public_key() -> dict:
    numbers = signing_key().public_key().public_numbers()
    return {"alg": "rsa-pkcs1v15-sha256", "n": format(numbers.n, "x"), "e": numbers.e}


def sign(data: bytes) -> str:
    return signing_key().sign(data, padding.PKCS1v15(), hashes.SHA256()).hex()


def _tar(entries: list[tuple[str, Path]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, path in entries:
            info = tarfile.TarInfo(name=name)
            # Scripts must keep LF endings to run in WSL, whatever git did on checkout.
            body = path.read_bytes().replace(b"\r\n", b"\n") if path.suffix == ".sh" else path.read_bytes()
            info.size = len(body)
            info.mode = 0o755 if path.suffix == ".sh" else 0o644
            info.mtime = 0
            tf.addfile(info, io.BytesIO(body))
    # mtime=0 keeps the gzip header, and so the hash, stable across restarts.
    return gzip.compress(raw.getvalue(), mtime=0)


def _pack(src: Path) -> bytes:
    files = sorted({p for pattern in _BUNDLE_FILES for p in src.glob(pattern) if p.is_file()})
    return _tar([(path.name, path) for path in files])


_INSTALL_FILES = (
    "scripts/setup-lab-node.sh",
    "supervisor/bootstrap.ps1",
    "supervisor/install.ps1",
    "supervisor/install.cmd",
    "supervisor/run-wsl-agent.ps1",
)


@lru_cache(maxsize=1)
def install_archive() -> bytes | None:
    """Everything a blank lab PC needs: agent/, the WSL installer, and the
    Windows boot-task scripts. Served to supervisor/bootstrap.ps1."""
    root = install_root()
    src = agent_dir()
    missing = [rel for rel in _INSTALL_FILES if not (root / rel).exists()]
    if missing or not (src / "agent.py").exists():
        return None
    agent_files = sorted({p for pattern in _BUNDLE_FILES for p in src.glob(pattern) if p.is_file()})
    entries = [("agent/" + p.name, p) for p in agent_files]
    entries += [(rel, root / rel) for rel in _INSTALL_FILES]
    return _tar(entries)


def bootstrap_script() -> str | None:
    """bootstrap.ps1 with this server's API and registry filled in as defaults."""
    path = install_root() / "supervisor" / "bootstrap.ps1"
    if not path.exists():
        return None
    return (
        path.read_text()
        .replace("__LAB_API__", settings.public_base_url.rstrip("/"))
        .replace("__LAB_REGISTRY__", settings.registry_url)
    )


@lru_cache(maxsize=1)
def current_bundle() -> Bundle | None:
    src = agent_dir()
    agent_py = src / "agent.py"
    if not agent_py.exists():
        return None
    match = _VERSION_RE.search(agent_py.read_text())
    if not match:
        return None
    data = _pack(src)
    return Bundle(version=match.group(1), data=data, sha256=hashlib.sha256(data).hexdigest(), signature=sign(data))


def parse_version(value: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in (value or "").strip().split("."))
    except ValueError:
        return None


def target_version() -> str | None:
    if settings.agent_target_version:
        return settings.agent_target_version
    bundle = current_bundle()
    return bundle.version if bundle else None


def is_outdated(agent_version: str | None) -> bool:
    """True when a real agent is below AGENT_MIN_VERSION. Unparsable versions
    (harness agents) are never blocked."""
    have = parse_version(agent_version)
    floor = parse_version(settings.agent_min_version) if settings.agent_min_version else None
    return bool(have and floor and have < floor)


def update_for(agent_version: str | None, update_ring: int) -> dict | None:
    """The update notice for a node's heartbeat, or None when it is current,
    outside the rollout ring, or the bundle on disk is not the target."""
    bundle = current_bundle()
    target = target_version()
    if bundle is None or target is None or bundle.version != target:
        return None
    if parse_version(agent_version) is None or agent_version == target:
        return None
    if update_ring > settings.agent_rollout_ring:
        return None
    return {"version": bundle.version, "sha256": bundle.sha256, "url": "/agent/bundle"}
