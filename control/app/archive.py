from __future__ import annotations

import io
import posixpath
import re
import tarfile
import zipfile
from dataclasses import dataclass


class ArchiveError(ValueError):
    pass


MAX_MEMBERS = 50_000
REQUIREMENTS_MAX_BYTES = 256 * 1024
# Folders that hold libraries or tool state, never a script the user means to run.
_SKIPPED_PARTS = {"__pycache__", "site-packages", "node_modules", "venv", ".venv", "__MACOSX"}
# Likely entrypoint names, most likely first.
_PREFERRED_STEMS = ("train", "main", "run", "app", "__main__", "start", "fit")
_SAFE_PY_NAME = re.compile(r"[^A-Za-z0-9_.\-]")


def _names_from_zip(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return [n for n in zf.namelist() if not n.endswith("/")]


def _names_from_tar(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        return [m.name for m in tf.getmembers() if m.isfile()]


def archive_kind(data: bytes, filename: str) -> str:
    """`zip`, `tar` (plain or compressed) or `py` (one script). The bytes decide
    first; the file name only tells a script from a damaged archive."""
    lower = (filename or "").lower()
    if data[:2] == b"PK":
        return "zip"
    if data[:2] == b"\x1f\x8b" or data[257:262] == b"ustar":
        return "tar"
    if lower.endswith(".py"):
        return "py"
    if lower.endswith(".zip"):
        return "zip"
    return "tar"


def wrap_script(filename: str, data: bytes) -> tuple[str, bytes]:
    """A lone .py upload as a one-file tar.gz; returns (script name, archive)."""
    base = re.split(r"[\\/]", filename or "")[-1]
    name = _SAFE_PY_NAME.sub("_", base)
    if not name.endswith(".py") or name == ".py":
        raise ArchiveError("a single file must be a .py script")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(data))
    return name, buf.getvalue()


def _clean(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name


def _check_safe(names: list[str]) -> None:
    if len(names) > MAX_MEMBERS:
        raise ArchiveError("archive has too many files")
    for name in names:
        if name.startswith("/") or ".." in name.split("/"):
            raise ArchiveError(f"archive contains an unsafe path: {name}")


def inspect_archive(data: bytes, filename: str) -> list[str]:
    kind = archive_kind(data, filename)
    try:
        names = _names_from_zip(data) if kind == "zip" else _names_from_tar(data)
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
        raise ArchiveError(f"archive could not be read: {exc}") from exc
    names = [_clean(n) for n in names]
    if not names:
        raise ArchiveError("archive is empty")
    _check_safe(names)
    return names


@dataclass(frozen=True)
class Upload:
    """What a submission is stored as: the bytes the agent will extract, the
    file names inside, and the script name for a single-file upload."""

    data: bytes
    names: list[str]
    extension: str  # zip | tar | tar.gz
    script: str | None = None


def prepare_upload(data: bytes, filename: str) -> Upload:
    kind = archive_kind(data, filename)
    if kind == "py":
        script, wrapped = wrap_script(filename, data)
        return Upload(wrapped, [script], "tar.gz", script)
    names = inspect_archive(data, filename)
    if kind == "zip":
        return Upload(data, names, "zip")
    return Upload(data, names, "tar.gz" if data[:2] == b"\x1f\x8b" else "tar")


def candidate_entrypoints(names: list[str]) -> list[str]:
    """The .py files worth offering, shallowest first."""
    found = []
    for name in names:
        parts = name.split("/")
        if not name.endswith(".py") or parts[-1] == "__init__.py":
            continue
        if any(p in _SKIPPED_PARTS or p.startswith(".") for p in parts):
            continue
        found.append(name)
    return sorted(found, key=lambda n: (n.count("/"), n))[:200]


def suggest_entrypoint(candidates: list[str]) -> str | None:
    def rank(name: str) -> tuple[int, int, str]:
        stem = posixpath.basename(name)[:-3]
        preferred = _PREFERRED_STEMS.index(stem) if stem in _PREFERRED_STEMS else len(_PREFERRED_STEMS)
        return preferred, name.count("/"), name

    return min(candidates, key=rank) if candidates else None


def _read_member(data: bytes, filename: str, wanted: str) -> bytes | None:
    """The first member whose cleaned name is `wanted`, up to REQUIREMENTS_MAX_BYTES."""
    try:
        if archive_kind(data, filename) == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if not info.is_dir() and _clean(info.filename) == wanted:
                        with zf.open(info) as fh:
                            return fh.read(REQUIREMENTS_MAX_BYTES)
            return None
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for member in tf:
                if member.isfile() and _clean(member.name) == wanted:
                    fh = tf.extractfile(member)
                    return fh.read(REQUIREMENTS_MAX_BYTES) if fh else None
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError):
        return None
    return None


def find_requirements(data: bytes, filename: str, names: list[str], entrypoint: str | None) -> str | None:
    """requirements.txt next to the entrypoint, at the root, or inside the single
    folder a zipped project usually has; None when absent or blank."""
    folders = []
    if entrypoint and "/" in entrypoint:
        folders.append(posixpath.dirname(entrypoint))
    folders.append("")
    tops = {n.split("/")[0] for n in names if "/" in n}
    if len(tops) == 1 and all("/" in n for n in names):
        folders.append(next(iter(tops)))
    for folder in folders:
        wanted = f"{folder}/requirements.txt" if folder else "requirements.txt"
        if wanted in names:
            raw = _read_member(data, filename, wanted)
            text = (raw or b"").decode("utf-8", errors="replace").strip()
            return text or None
    return None


def entrypoint_in_archive(names: list[str], entrypoint: str) -> str:
    wanted = entrypoint.lstrip("./")
    exact = [n for n in names if n == wanted or n.endswith("/" + wanted)]
    if wanted in names:
        return wanted
    # Single top-level directory: foo/train.py
    tops = {n.split("/")[0] for n in names}
    if len(tops) == 1:
        nested = f"{next(iter(tops))}/{wanted}"
        if nested in names:
            return nested
    if exact:
        return exact[0]
    raise ArchiveError(f"entrypoint {entrypoint!r} not found in archive")
