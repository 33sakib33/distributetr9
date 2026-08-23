from __future__ import annotations

import io
import tarfile
import zipfile


class ArchiveError(ValueError):
    pass


def _names_from_zip(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return [n for n in zf.namelist() if not n.endswith("/")]


def _names_from_tar(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        return [m.name for m in tf.getmembers() if m.isfile()]


def inspect_archive(data: bytes, filename: str) -> list[str]:
    lower = filename.lower()
    try:
        if lower.endswith(".zip"):
            names = _names_from_zip(data)
        elif lower.endswith(".tar.gz") or lower.endswith(".tgz") or lower.endswith(".tar"):
            names = _names_from_tar(data)
        else:
            # Peek magic.
            if data[:2] == b"PK":
                names = _names_from_zip(data)
            else:
                names = _names_from_tar(data)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"archive could not be read: {exc}") from exc
    if not names:
        raise ArchiveError("archive is empty")
    return names


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
