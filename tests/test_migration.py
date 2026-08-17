"""Tests for the one-time migration of pre-0.2.0 data into ~/.local/ptk/."""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from ai_coding_usage_tracker import paths


def _legacy_layout(home: Path) -> None:
    state = home / ".local" / "state" / "plantrack"
    (state / "payloads").mkdir(parents=True)
    (state / "plantrack.db").write_text("db", encoding="utf-8")
    (state / "payloads" / "zai-quota-limit.json").write_text("{}", encoding="utf-8")
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "plantrack-claude-rate-limits.json").write_text("{}", encoding="utf-8")
    (claude / "plantrack-claude-profile.json").write_text("{}", encoding="utf-8")


def test_legacy_data_moved_into_ptk_dir(tmp_path: Path) -> None:
    _legacy_layout(tmp_path)
    paths.migrate_legacy(tmp_path)
    data = paths.ptk_data_dir(tmp_path)
    assert (data / "plantrack.db").read_text(encoding="utf-8") == "db"
    assert (data / "payloads" / "zai-quota-limit.json").exists()
    assert (data / "claude-rate-limits.json").exists()
    assert (data / "claude-profile.json").exists()
    # Old locations are emptied.
    assert not (tmp_path / ".local" / "state" / "plantrack" / "plantrack.db").exists()
    assert not (tmp_path / ".claude" / "plantrack-claude-rate-limits.json").exists()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    _legacy_layout(tmp_path)
    paths.migrate_legacy(tmp_path)
    data = paths.ptk_data_dir(tmp_path)
    (data / "plantrack.db").write_text("new-db", encoding="utf-8")
    paths.migrate_legacy(tmp_path)  # no-op: targets exist
    assert (data / "plantrack.db").read_text(encoding="utf-8") == "new-db"


def test_migration_never_overwrites_existing_target(tmp_path: Path) -> None:
    legacy = tmp_path / ".local" / "state" / "plantrack"
    legacy.mkdir(parents=True)
    (legacy / "plantrack.db").write_text("old", encoding="utf-8")
    data = paths.ptk_data_dir(tmp_path)
    data.mkdir(parents=True)
    (data / "plantrack.db").write_text("current", encoding="utf-8")
    paths.migrate_legacy(tmp_path)
    assert (data / "plantrack.db").read_text(encoding="utf-8") == "current"


def test_migration_on_clean_home_is_noop(tmp_path: Path) -> None:
    paths.migrate_legacy(tmp_path)
    assert not paths.ptk_data_dir(tmp_path).exists()


def test_cli_callback_runs_migration(tmp_path: Path) -> None:
    """The app callback must migrate the default home before commands run.

    Regression guard: deleting the migrate_legacy call from cli.py would
    otherwise leave every test green.
    """
    from ai_coding_usage_tracker.cli import app

    _legacy_layout(tmp_path)  # tmp_path is the pinned PLANTRACK_HOME
    result = CliRunner().invoke(app, ["plan", "list"])
    assert result.exit_code == 0
    assert (tmp_path / ".local" / "ptk" / "plantrack.db").read_text(encoding="utf-8") == "db"
    assert not (tmp_path / ".local" / "state" / "plantrack" / "plantrack.db").exists()


def test_version_flag_skips_migration(tmp_path: Path) -> None:
    """--version exits eagerly: no home access, nothing migrated or created."""
    from ai_coding_usage_tracker.cli import app

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert not (tmp_path / ".local" / "ptk").exists()
    assert os.environ.get("PLANTRACK_HOME") == str(tmp_path)
