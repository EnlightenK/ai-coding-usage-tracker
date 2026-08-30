"""Scan of the local PC for coding tool configs, credentials and usage logs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, paths
from .discovery import DiscoveredPlan, discover_plans


@dataclass(frozen=True)
class ScanEntry:
    """One thing plantrack looked for on the local machine."""

    label: str
    path: str
    found: bool
    kind: str  # "file" for configs/credentials, "logs" for usage stores
    size: int | None = None
    modified: datetime | None = None
    count: int | None = None


def collect_scan(
    home: Path | None = None,
) -> tuple[list[ScanEntry], list[ScanEntry], list[DiscoveredPlan]]:
    """Return (config files, usage log stores, discovered plans) for one home."""
    home = home or paths.default_home()
    return _file_entries(home), _log_entries(home), discover_plans(home)


def _file_entries(home: Path) -> list[ScanEntry]:
    candidates = [
        ("Claude Code credentials", paths.claude_dir(home) / ".credentials.json"),
        ("Claude Code settings", paths.claude_dir(home) / "settings.json"),
        ("Claude MiniMax CN settings", paths.claude_dir(home) / "settings-mx-cn.json"),
        ("Claude GLM settings", paths.claude_dir(home) / "settings-glm.json"),
        ("Codex credentials", paths.codex_dir(home) / "auth.json"),
        ("Codex config", paths.codex_dir(home) / "config.toml"),
        ("OpenCode auth", paths.opencode_auth_file(home)),
        ("plantrack config", config.config_file(home)),
    ]
    entries: list[ScanEntry] = []
    for label, path in candidates:
        found = path.is_file()
        size: int | None = None
        modified: datetime | None = None
        if found:
            try:
                stat = path.stat()
            except OSError:
                found = False
            else:
                size = stat.st_size
                modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        entries.append(ScanEntry(label, str(path), found, "file", size=size, modified=modified))
    return entries


def _log_entries(home: Path) -> list[ScanEntry]:
    stores = [
        ("Claude Code transcripts", paths.claude_dir(home) / "projects", "*.jsonl"),
        ("Codex sessions", paths.codex_dir(home) / "sessions", "*.jsonl"),
        ("Codex archived sessions", paths.codex_dir(home) / "archived_sessions", "*.jsonl"),
        ("OpenCode messages", paths.opencode_data_dir(home) / "storage" / "message", "*.json"),
        ("OpenCode parts", paths.opencode_data_dir(home) / "storage" / "part", "*.json"),
    ]
    entries: list[ScanEntry] = []
    for label, directory, pattern in stores:
        if not directory.is_dir():
            entries.append(ScanEntry(label, str(directory), False, "logs"))
            continue
        count = sum(1 for _ in directory.rglob(pattern))
        entries.append(ScanEntry(label, str(directory), True, "logs", count=count))
    return entries
