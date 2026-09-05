"""Tests for restrictive, atomic writes of secret- and account-data files."""

from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path

import pytest

from ai_coding_usage_tracker import config, fileutil, payload_dump, store
from ai_coding_usage_tracker.providers import claude_limits, claude_profile

ChmodCalls = list[tuple[Path, int]]


@pytest.fixture
def chmod_calls(monkeypatch: pytest.MonkeyPatch) -> ChmodCalls:
    """Record every chmod issued through fileutil without touching the fs."""
    calls: ChmodCalls = []

    def record(path: object, mode: int) -> None:
        calls.append((Path(os.fspath(path)), mode))

    monkeypatch.setattr(fileutil.os, "chmod", record)
    return calls


@pytest.fixture
def posix_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the host is POSIX so fileutil applies permission bits."""
    monkeypatch.setattr(fileutil, "_is_posix", lambda: True)


def test_non_posix_write_skips_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chmod_calls: ChmodCalls
) -> None:
    monkeypatch.setattr(fileutil, "_is_posix", lambda: False)
    target = tmp_path / "secrets.json"
    assert fileutil.secure_write_text(target, "token")
    assert target.read_text(encoding="utf-8") == "token"
    fileutil.secure_dir(tmp_path / "nested")
    assert chmod_calls == []


def test_default_windows_path_never_chmods(tmp_path: Path, chmod_calls: ChmodCalls) -> None:
    if os.name != "nt":
        pytest.skip("host default is already POSIX; covered by the patched test")
    assert fileutil.secure_write_text(tmp_path / "secrets.json", "token")
    fileutil.secure_dir(tmp_path / "nested")
    assert chmod_calls == []


def test_posix_write_chmods_file_0600_and_dir_0700(
    tmp_path: Path, posix_only: None, chmod_calls: ChmodCalls
) -> None:
    target = tmp_path / ".claude" / "cache.json"
    assert fileutil.secure_write_text(target, "token")
    assert target.read_text(encoding="utf-8") == "token"
    assert (target, 0o600) in chmod_calls
    assert (target.parent, 0o700) in chmod_calls


def test_posix_secure_dir_creates_and_locks(
    tmp_path: Path, posix_only: None, chmod_calls: ChmodCalls
) -> None:
    deep = tmp_path / ".config" / "plantrack"
    fileutil.secure_dir(deep)
    assert deep.is_dir()
    assert (deep, 0o700) in chmod_calls


def test_write_creates_missing_nested_dirs(tmp_path: Path) -> None:
    target = tmp_path / ".local" / "ptk" / "payloads" / "x.json"
    assert fileutil.secure_write_text(target, "{}")
    assert target.read_text(encoding="utf-8") == "{}"


def test_write_is_atomic_and_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "cache.json"
    assert fileutil.secure_write_text(target, '{"v": 1}')
    assert fileutil.secure_write_text(target, '{"v": 2}')
    assert target.read_text(encoding="utf-8") == '{"v": 2}'
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]


def test_write_failure_returns_false_and_cleans_tmp(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    assert not fileutil.secure_write_text(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["occupied"]


def test_write_replaces_a_symlink_instead_of_following_it(tmp_path: Path) -> None:
    """A symlink planted at the target must not redirect the write."""
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    target = tmp_path / "cache.json"
    target.symlink_to(outside)
    assert fileutil.secure_write_text(target, "secret")
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "secret"
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_concurrent_writers_do_not_share_a_staging_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writers of one target must stage in distinct files.

    A cron `status` overlapping an interactive one is the deployment the
    README recommends, and both used to write `<name>.tmp` - so one process
    scribbled into the other's half-finished staging file.
    """
    target = tmp_path / "cache.json"
    staged: list[str] = []
    real_mkstemp = fileutil.tempfile.mkstemp

    def spy(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        staged.append(name)
        # Re-entering here stands in for the overlapping process: it must not
        # be handed the staging name this call is still holding.
        if len(staged) == 1:
            assert fileutil.secure_write_text(target, "from the other process")
        return fd, name

    monkeypatch.setattr(fileutil.tempfile, "mkstemp", spy)
    assert fileutil.secure_write_text(target, "from this process")

    assert len(staged) == 2
    assert staged[0] != staged[1]
    # Both writes completed; the last to rename wins, intact and whole.
    assert target.read_text(encoding="utf-8") == "from this process"
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]


def test_staging_file_is_created_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The content is 0600 from the first byte, not only after the rename."""
    modes: list[int] = []
    real_mkstemp = fileutil.tempfile.mkstemp

    def spy(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        modes.append(os.stat(name).st_mode & 0o777)
        return fd, name

    monkeypatch.setattr(fileutil.tempfile, "mkstemp", spy)
    target = tmp_path / "cache.json"
    assert fileutil.secure_write_text(target, "secret")
    assert modes == [0o600]
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_write_leaks_no_descriptor_or_staging_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure between mkstemp and the rename must clean up both.

    mkstemp hands back a raw descriptor, so the error path has to close it
    itself - otherwise a long-lived `plantrack` process leaks one per write.
    """
    opened: list[int] = []

    def failing_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        opened.append(fd)
        raise OSError("cannot wrap the descriptor")

    monkeypatch.setattr(fileutil.os, "fdopen", failing_fdopen)
    target = tmp_path / "cache.json"
    assert not fileutil.secure_write_text(target, "secret")
    # Checked before anything else can claim the number back.
    assert opened
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert list(tmp_path.iterdir()) == []


def test_write_cleans_up_when_the_content_cannot_be_encoded(tmp_path: Path) -> None:
    """A non-OSError failure mid-write still takes the staging file with it."""
    target = tmp_path / "cache.json"
    with pytest.raises(UnicodeEncodeError):
        fileutil.secure_write_text(target, "lone surrogate: \ud800")
    assert list(tmp_path.iterdir()) == []


def test_save_config_returns_true_and_keeps_format(
    tmp_path: Path, posix_only: None, chmod_calls: ChmodCalls
) -> None:
    payload = {"disabled": ["minimax-intl"], "api_keys": {"glm": {"api_key": "k1"}}}
    assert config.save_config(tmp_path, payload)
    target = tmp_path / ".config" / "plantrack" / "config.json"
    assert target.read_text(encoding="utf-8") == json.dumps(payload, indent=2) + "\n"
    assert (target, 0o600) in chmod_calls
    assert (target.parent, 0o700) in chmod_calls


def test_dump_enabled_returns_true_and_keeps_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, posix_only: None, chmod_calls: ChmodCalls
) -> None:
    monkeypatch.setenv(payload_dump.DUMP_ENV, "1")
    assert payload_dump.dump("claude-org-usage", {"remaining": 3}, tmp_path)
    target = tmp_path / ".local" / "ptk" / "payloads" / "claude-org-usage.json"
    wrapper = json.loads(target.read_text(encoding="utf-8"))
    assert wrapper["payload"] == {"remaining": 3}
    assert isinstance(wrapper["_fetched_at"], str)
    assert (target, 0o600) in chmod_calls


def test_dump_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(payload_dump.DUMP_ENV, raising=False)
    assert not payload_dump.dump("x", {}, tmp_path)


def test_store_connect_chmods_db_on_posix(
    tmp_path: Path, posix_only: None, chmod_calls: ChmodCalls
) -> None:
    with closing(store._connect(tmp_path)) as conn:
        conn.execute("SELECT 1")
    target = store.db_path(tmp_path)
    assert target.is_file()
    assert (target, 0o600) in chmod_calls
    assert (target.parent, 0o700) in chmod_calls


def test_claude_writers_use_secure_write(
    tmp_path: Path, posix_only: None, chmod_calls: ChmodCalls
) -> None:
    assert claude_limits.capture_windows({"five_hour": {"used_percentage": 10}}, tmp_path)
    limits_target = claude_limits.cache_file(tmp_path)
    assert limits_target.is_file()
    assert (limits_target, 0o600) in chmod_calls

    assert claude_profile._write_cache({"account": {"email": "e"}}, tmp_path)
    profile_target = claude_profile.cache_file(tmp_path)
    assert json.loads(profile_target.read_text(encoding="utf-8"))["profile"] == {
        "account": {"email": "e"}
    }
    assert (profile_target, 0o600) in chmod_calls
    assert (claude_limits.cache_file(tmp_path).parent, 0o700) in chmod_calls
