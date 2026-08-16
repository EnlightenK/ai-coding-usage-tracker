"""Restrictive, atomic writes for files that hold secrets or account data."""

from __future__ import annotations

import os
from pathlib import Path

FILE_MODE = 0o600
DIR_MODE = 0o700


def _is_posix() -> bool:
    """Whether the current platform honors POSIX permission bits."""
    return os.name != "nt"


def secure_dir(path: Path) -> None:
    """Create `path` with parents and restrict it to the owner on POSIX."""
    path.mkdir(parents=True, exist_ok=True)
    if _is_posix():
        try:
            os.chmod(path, DIR_MODE)
        except OSError:
            pass


def secure_file(path: Path) -> None:
    """Restrict an existing file to its owner on POSIX (best-effort)."""
    if not _is_posix():
        return
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def secure_write_text(path: Path, content: str) -> bool:
    """Atomically write `content` to `path`, owner-only on POSIX.

    The text lands in a sibling temp file first and is moved onto the target
    with os.replace, so readers never see a half-written file and a symlink
    at the target is replaced rather than followed. Returns False on OSError.
    """
    tmp = path.parent / (path.name + ".tmp")
    try:
        secure_dir(path.parent)
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    secure_file(path)
    return True
