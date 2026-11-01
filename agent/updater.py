"""Agent self-update. Stdlib only.

Install layout (made by scripts/setup-lab-node.sh):

    /opt/lab-agent/
      releases/0.6.1/agent.py ...
      releases/0.7.0/agent.py ...
      current -> releases/0.7.0
      pending              # JSON marker while a new release is unproven

The launcher (`lab-agent`, see launcher.sh) runs current/run.sh in a loop. An
update unpacks into releases/<version>, switches `current` atomically, and
exits with EXIT_UPDATED so the launcher restarts on the new code. The new
release deletes `pending` once it has registered; if it keeps crashing first,
the launcher points `current` back at the previous release.
"""
from __future__ import annotations

import compileall
import gzip
import hashlib
import hmac
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

EXIT_UPDATED = 75
# DER prefix of a PKCS#1 v1.5 DigestInfo for SHA-256.
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class UpdateError(Exception):
    pass


def verify_signature(data: bytes, signature_hex: str, public_key: dict) -> bool:
    """RSA PKCS#1 v1.5 / SHA-256 signature check with plain integer math."""
    try:
        n = int(public_key["n"], 16)
        e = int(public_key["e"])
        sig = bytes.fromhex(signature_hex)
    except (KeyError, TypeError, ValueError):
        return False
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    digest = _SHA256_DIGEST_INFO + hashlib.sha256(data).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(em, expected)


def release_root(code_dir: Path | None = None) -> Path | None:
    """The install root when running from releases/<version>, else None."""
    here = (code_dir or Path(__file__).resolve().parent).resolve()
    if here.parent.name == "releases":
        return here.parent.parent
    return None


def _safe_extract(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r") as tf:
        for member in tf.getmembers():
            if not member.isfile() or "/" in member.name or member.name.startswith("."):
                raise UpdateError("unexpected entry in bundle: %s" % member.name)
        try:
            tf.extractall(dest, filter="data")
        except TypeError:
            # Older Pythons lack filter=; members were checked flat and regular above.
            tf.extractall(dest)


def _switch_current(root: Path, release: str) -> None:
    tmp = root / "current.new"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(Path("releases") / release)
    os.replace(tmp, root / "current")


def apply_update(fetch, update: dict, public_key: dict, root: Path, running_version: str) -> str:
    """Download, verify, unpack and activate `update`. Returns the new version.

    `fetch()` returns (bundle_bytes, headers) for the bundle URL.
    """
    version = str(update.get("version") or "")
    if not version or "/" in version or version.startswith("."):
        raise UpdateError("bad version in update notice")
    if version in rejected_versions(root):
        raise UpdateError("version %s was rolled back on this node" % version)
    data, headers = fetch()
    sha = hashlib.sha256(data).hexdigest()
    if sha != update.get("sha256") or sha != headers.get("X-Bundle-Sha256"):
        raise UpdateError("bundle checksum mismatch")
    if headers.get("X-Agent-Version") != version:
        raise UpdateError("bundle version mismatch")
    if not verify_signature(data, headers.get("X-Bundle-Signature", ""), public_key):
        raise UpdateError("bundle signature does not verify")

    releases = root / "releases"
    staging = releases / (version + ".tmp")
    final = releases / version
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        _safe_extract(data, staging)
        if not (staging / "agent.py").exists():
            raise UpdateError("bundle has no agent.py")
        if not compileall.compile_dir(str(staging), quiet=1, force=True):
            raise UpdateError("bundle does not compile")
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    previous = (root / "current").resolve().name if (root / "current").exists() else running_version
    (root / "pending").write_text(json.dumps({"version": version, "previous": previous}))
    _switch_current(root, version)
    return version


def rejected_versions(root: Path) -> set[str]:
    try:
        return {line.strip() for line in (root / "rejected").read_text().splitlines() if line.strip()}
    except OSError:
        return set()


def confirm_running(root: Path | None, version: str) -> bool:
    """Called after a successful register: the new release works, keep it."""
    if root is None:
        return False
    marker = root / "pending"
    try:
        pending = json.loads(marker.read_text())
    except (OSError, ValueError):
        return False
    if pending.get("version") != version:
        return False
    marker.unlink(missing_ok=True)
    (root / "pending.fails").unlink(missing_ok=True)
    return True


def prune_releases(root: Path, keep: int = 3) -> None:
    current = (root / "current").resolve().name
    releases = sorted(
        (p for p in (root / "releases").iterdir() if p.is_dir() and not p.name.endswith(".tmp")),
        key=lambda p: p.stat().st_mtime,
    )
    for old in releases[:-keep]:
        if old.name != current:
            shutil.rmtree(old, ignore_errors=True)
