"""Resolution of well-known configuration directories for each coding tool."""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "PLANTRACK_HOME"


def default_home() -> Path:
    """Return the home directory used for discovery (override with PLANTRACK_HOME)."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override)
    return Path.home()


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
