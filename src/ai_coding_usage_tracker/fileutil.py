"""Restrictive, atomic writes for files that hold secrets or account data."""

from __future__ import annotations

import os
import tempfile
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

    The text lands in a private temp file in the target's own directory and is
    moved onto the target with os.replace, so readers never see a half-written
    file and a symlink at the target is replaced rather than followed. The
    staging name comes from tempfile.mkstemp, so two writers of the same target
    - a cron `status` overlapping an interactive one - never share it; mkstemp
    also creates the file 0600, so the content is owner-only from the first
    byte rather than from the chmod after the rename. Returns False on OSError.
    """
    tmp: str | None = None
    try:
        try:
            secure_dir(path.parent)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
            try:
                handle = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                # fdopen did not take ownership of the descriptor, so close it
                # here; leaving it open would leak the fd for the process life.
                os.close(fd)
                raise
            with handle:
                handle.write(content)
            os.replace(tmp, path)
        except BaseException:
            # Any failure, not just OSError, must take the staging file with it.
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise
    except OSError:
        return False
    secure_file(path)
    return True
