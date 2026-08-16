"""Tests for Claude rate limit capture and refresh flows."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from ai_coding_usage_tracker.providers import claude, claude_limits

STATUSLINE_PAYLOAD = {
    "session_id": "abc123",
    "model": {"id": "claude-opus-5", "display_name": "Opus"},
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": int(time.time()) + 7200},
        "seven_day": {"used_percentage": 41.2, "resets_at": int(time.time()) + 86400},
    },
}


def test_capture_and_load_roundtrip(home: Path) -> None:
    assert claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    snapshot = claude_limits.load_cached(home)
    assert snapshot is not None
    assert snapshot["five_hour"]["used_percentage"] == 23.5
    assert snapshot["seven_day"]["used_percentage"] == 41.2


def test_capture_without_rate_limits_is_noop(home: Path) -> None:
    assert not claude_limits.capture_from_statusline_json({"model": {}}, home)
    assert claude_limits.load_cached(home) is None


def test_cached_quotas_converted(home: Path) -> None:
    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert set(quotas) == {"5h", "weekly"}
    assert quotas["5h"].remaining_percent == 76.5
    assert quotas["weekly"].remaining_percent == 58.8
    assert quotas["5h"].resets_at is not None


def test_stale_cache_ignored(home: Path) -> None:
    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    target = claude_limits.cache_file(home)
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    snapshot["captured_at"] = "2020-01-01T00:00:00+00:00"
    target.write_text(json.dumps(snapshot), encoding="utf-8")
    assert claude_limits.load_cached(home) is None
    assert claude.cached_quotas(home) == []


def test_missing_cache_returns_empty(home: Path) -> None:
    assert claude_limits.load_cached(home) is None
    assert claude.cached_quotas(home) == []


def test_captured_age_formats(home: Path) -> None:
    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    age = claude_limits.captured_age(home)
    assert age is not None
    assert age.endswith(("s", "m"))


def test_captured_age_missing_cache(home: Path) -> None:
    assert claude_limits.captured_age(home) is None


def test_tracker_notes_fresh_capture(home: Path) -> None:
    from ai_coding_usage_tracker.tracker import collect_statuses

    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    statuses = {s.plan_id: s for s in collect_statuses(home)}
    claude_status = statuses["claude-code"]
    assert "rate limits as of" in (claude_status.note or "")
    assert claude_status.quotas


def test_tracker_notes_stale_capture(home: Path) -> None:
    from ai_coding_usage_tracker.tracker import collect_statuses

    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    target = claude_limits.cache_file(home)
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    snapshot["captured_at"] = "2020-01-01T00:00:00+00:00"
    target.write_text(json.dumps(snapshot), encoding="utf-8")
    statuses = {s.plan_id: s for s in collect_statuses(home)}
    claude_status = statuses["claude-code"]
    assert "stale capture" in (claude_status.note or "")
    assert not claude_status.quotas


def test_session_key_from_env(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    assert claude_limits.load_session_key(home) == "sk-ant-sid01-test"


def test_session_key_from_data_dir_default(home: Path) -> None:
    """With no overrides the key is read from ~/.local/ptk/session-key."""
    key_file = claude_limits.session_key_file(home)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("sk-ant-sid02-default\n", encoding="utf-8")
    assert claude_limits.load_session_key(home) == "sk-ant-sid02-default"


def test_session_key_from_env_file_override(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "anywhere.key"
    key_file.write_text("sk-ant-sid03-file\n", encoding="utf-8")
    monkeypatch.setenv("PLANTRACK_SESSION_KEY_FILE", str(key_file))
    assert claude_limits.load_session_key(home) == "sk-ant-sid03-file"


def test_session_key_from_config_entry(home: Path, tmp_path: Path) -> None:
    """A `session_key_file` entry in the plantrack config opts into any path."""
    key_file = tmp_path / "repo-shared.key"
    key_file.write_text("sk-ant-sid04-config\n", encoding="utf-8")
    config_dir = home / ".config" / "plantrack"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"session_key_file": str(key_file)}), encoding="utf-8"
    )
    assert claude_limits.load_session_key(home) == "sk-ant-sid04-config"


def test_session_key_absent(home: Path) -> None:
    assert claude_limits.load_session_key(home) is None


def test_refresh_without_key(home: Path) -> None:
    success, note = claude_limits.refresh_from_api(home)
    assert not success
    assert "no claude.ai session key" in (note or "")


def test_refresh_success_via_api(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    def fake_get(url: str, headers: dict, timeout: float) -> FakeResponse:
        assert headers["Authorization"] == "Bearer sk-ant-sid01-test"
        if url.endswith("/oauth/profile"):
            return FakeResponse(
                200,
                {"organization": {"uuid": "org-123"}},
            )
        if url.endswith("/rate_limits"):
            return FakeResponse(
                200,
                {
                    "five_hour": {"used_percentage": 40.0, "resets_at": 1786870200},
                    "seven_day": {"used_percentage": 20.0, "resets_at": 1786881600},
                },
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = claude_limits.refresh_from_api(home)
    assert success
    assert note is None
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert quotas["5h"].remaining_percent == 60.0
    assert quotas["weekly"].remaining_percent == 80.0


def test_refresh_falls_back_to_usage_segment(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    def fake_get(url: str, headers: dict, timeout: float) -> FakeResponse:
        if url.endswith("/oauth/profile"):
            return FakeResponse(200, {"organization": {"uuid": "org-123"}})
        if url.endswith("/rate_limits"):
            return FakeResponse(403, {})
        if url.endswith("/usage"):
            return FakeResponse(
                200,
                {"data": {"five_hour": {"used_percentage": 55.0}}},
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = claude_limits.refresh_from_api(home)
    assert success
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert quotas["5h"].remaining_percent == 45.0


def test_refresh_rejected_session(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-bad")

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    def fake_get(url: str, headers: dict, timeout: float) -> FakeResponse:
        if url.endswith("/oauth/profile"):
            return FakeResponse(200, {"organization": {"uuid": "org-123"}})
        return FakeResponse(403, {})

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = claude_limits.refresh_from_api(home)
    assert not success
    assert "rejected" in (note or "")


def test_normalize_window_tolerates_variants() -> None:
    entry = claude_limits._normalize_window(
        {"used_percent": "bad", "usage_percentage": 33, "reset_at": "2026-08-16T20:00:00+00:00"}
    )
    assert entry is not None
    assert entry["used_percentage"] == 33.0
    assert entry["resets_at"] == int(
        datetime.fromisoformat("2026-08-16T20:00:00+00:00").timestamp()
    )
