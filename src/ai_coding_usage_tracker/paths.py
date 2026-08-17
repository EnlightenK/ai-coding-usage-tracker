"""Resolution of well-known configuration directories for each coding tool."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import fileutil

HOME_ENV = "PLANTRACK_HOME"

# Layout used before the ~/.local/ptk data home existed (kept for migration).
_LEGACY_STATE_DIR = (".local", "state", "plantrack")
_LEGACY_CLAUDE_CACHES = {
    "plantrack-claude-rate-limits.json": "claude-rate-limits.json",
    "plantrack-claude-profile.json": "claude-profile.json",
}


def default_home() -> Path:
    """Return the home directory used for discovery (override with PLANTRACK_HOME)."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override)
    return Path.home()


def ptk_data_dir(home: Path) -> Path:
    """The tool's own data home: database, caches, payload dumps, session key."""
    return home / ".local" / "ptk"


def claude_dir(home: Path) -> Path:
    return home / ".claude"


def codex_dir(home: Path) -> Path:
    return home / ".codex"


def opencode_config_dir(home: Path) -> Path:
    return home / ".config" / "opencode"


def opencode_data_dir(home: Path) -> Path:
    return home / ".local" / "share" / "opencode"


def opencode_auth_file(home: Path) -> Path:
    return opencode_data_dir(home) / "auth.json"


def migrate_legacy(home: Path | None = None) -> None:
    """One-time move of pre-0.2.0 data into ~/.local/ptk/ (best effort).

    Never overwrites: a file is moved only when the target does not exist yet.
    """
    home = home or default_home()
    data = ptk_data_dir(home)
    legacy_state = home.joinpath(*_LEGACY_STATE_DIR)
    _move_if_absent(legacy_state / "plantrack.db", data / "plantrack.db")
    _move_tree_if_absent(legacy_state / "payloads", data / "payloads")
    for old_name, new_name in _LEGACY_CLAUDE_CACHES.items():
        _move_if_absent(claude_dir(home) / old_name, data / new_name)


def _move_if_absent(source: Path, target: Path) -> None:
    # Check-then-act, not atomic: two concurrent first runs could race, and
    # the worst case is losing a few minutes of freshly recorded data — once.
    # Accepted for a documented best-effort, one-time migration.
    if not source.is_file() or target.exists():
        return
    try:
        fileutil.secure_dir(target.parent)
        shutil.move(str(source), str(target))
    except OSError:
        pass


def _move_tree_if_absent(source: Path, target: Path) -> None:
    if not source.is_dir() or target.exists():
        return
    try:
        fileutil.secure_dir(target.parent)
        shutil.move(str(source), str(target))
    except OSError:
        pass
