"""Tests for Claude rate limit capture and refresh flows."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import REAL_REFRESH_FROM_API, claude_profile_payload, write_profile_cache

from ai_coding_usage_tracker.providers import claude, claude_limits


def _future_ts(hours: float) -> int:
    """A reset epoch in the future, relative to now.

    Hardcoded reset dates silently drift into the past as a fixture ages, at
    which point the window reads as already reset and its percentage is
    correctly dropped - so the test starts asserting the wrong thing.
    """
    return int((datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp())


def _future_iso(hours: float) -> str:
    """A reset time in the future as an ISO string (see `_future_ts`)."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


# Real organization uuids are canonical UUIDs, and only those are accepted:
# the value is spliced into an authenticated request path.
ORG_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
CACHED_ORG_UUID = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"

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
    _age_snapshot(home, "2020-01-01T00:00:00+00:00")
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
    assert claude_limits.captured_at(home) is None


def test_captured_at_survives_a_stale_snapshot(home: Path) -> None:
    """The age must stay reportable after the windows themselves expire."""
    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    _age_snapshot(home, "2020-01-01T00:00:00+00:00")
    captured = claude_limits.captured_at(home)
    assert captured is not None and captured.year == 2020


def test_tracker_reports_fresh_capture(home: Path) -> None:
    from ai_coding_usage_tracker.tracker import collect_statuses

    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    statuses = {s.plan_id: s for s in collect_statuses(home)}
    claude_status = statuses["claude-code"]
    assert claude_status.quotas
    assert claude_status.quotas_source == claude_limits.SOURCE_STATUSLINE
    assert claude_status.quotas_captured_at is not None


def test_tracker_reports_stale_capture(home: Path) -> None:
    from ai_coding_usage_tracker.tracker import collect_statuses

    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    _age_snapshot(home, "2020-01-01T00:00:00+00:00")
    statuses = {s.plan_id: s for s in collect_statuses(home)}
    claude_status = statuses["claude-code"]
    assert not claude_status.quotas
    # The timestamp survives even though the windows are dropped, so the
    # table can explain *why* they are missing.
    assert claude_status.quotas_captured_at is not None
    assert claude_status.quotas_source is None


def test_refresh_reaches_a_cached_status_row(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh must be visible to the next `status`, not hidden by the cache.

    The quota windows live in a local snapshot file, so a status row cached
    from an earlier capture must never outrank what is on disk now.
    """
    from ai_coding_usage_tracker.tracker import collect_statuses

    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    first = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    assert {q.kind: q.remaining_percent for q in first.quotas}["5h"] == 76.5

    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    monkeypatch.setattr(
        claude_limits.requests,
        "get",
        _fake_usage_api({"five_hour": {"utilization": 63.0}}),
    )
    assert REAL_REFRESH_FROM_API(home)[0]

    # Well inside the status cache TTL: the row is a hit, and must still
    # report the number the refresh just wrote.
    second = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    assert "cached" in (second.note or "")
    assert {q.kind: q.remaining_percent for q in second.quotas}["5h"] == 37.0
    assert second.quotas_source == claude_limits.SOURCE_ACCOUNT_SESSION


def test_history_records_the_capture_source(home: Path) -> None:
    """A history row must still say which channel produced the numbers."""
    from ai_coding_usage_tracker import store
    from ai_coding_usage_tracker.tracker import collect_statuses

    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    collect_statuses(home)
    rows = {r["plan_id"]: r for r in store.status_history(home, hours=1)}
    assert claude_limits.SOURCE_STATUSLINE in (rows["claude-code"]["note"] or "")


def test_cached_row_drops_a_stored_capture_phrase(home: Path) -> None:
    """A row cached by an older version must not print two capture ages."""
    from ai_coding_usage_tracker import store
    from ai_coding_usage_tracker.models import PlanStatus
    from ai_coding_usage_tracker.tracker import collect_statuses

    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    store.record_status(
        home,
        [
            PlanStatus(
                plan_id="claude-code",
                provider="Anthropic",
                name="Claude Code (Anthropic)",
                region=None,
                auth_kind="oauth",
                configured=True,
                active=True,
                note="rate limits as of 42s ago (statusline capture); subscription canceled",
            )
        ],
    )
    status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    assert "cached" in (status.note or "")
    assert "rate limits as of" not in (status.note or "")
    # Unrelated parts of the stored note survive the strip.
    assert "subscription canceled" in (status.note or "")
    assert status.quotas_captured_at is not None


def _age_snapshot(home: Path, captured_at: str) -> None:
    """Backdate the cached snapshot to a given ISO timestamp."""
    target = claude_limits.cache_file(home)
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    snapshot["captured_at"] = captured_at
    target.write_text(json.dumps(snapshot), encoding="utf-8")


def test_session_key_from_env(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A decoy file at the default location ensures precedence is really pinned.
    default = claude_limits.session_key_file(home)
    default.parent.mkdir(parents=True, exist_ok=True)
    default.write_text("sk-ant-decoy-default\n", encoding="utf-8")
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


def test_session_key_env_file_is_tilde_expanded(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env overrides often come from systemd/cron/.env where ~ is not expanded."""
    key_file = tmp_path / "repo.key"
    key_file.write_text("sk-ant-sid05-tilde\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("PLANTRACK_SESSION_KEY_FILE", "~/repo.key")
    assert claude_limits.load_session_key(home) == "sk-ant-sid05-tilde"


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


def test_session_key_env_file_beats_config_entry(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_key = tmp_path / "config-key"
    config_key.write_text("sk-ant-config\n", encoding="utf-8")
    env_key = tmp_path / "env-key"
    env_key.write_text("sk-ant-env\n", encoding="utf-8")
    config_dir = home / ".config" / "plantrack"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"session_key_file": str(config_key)}), encoding="utf-8"
    )
    monkeypatch.setenv(claude_limits.SESSION_KEY_FILE_ENV, str(env_key))
    assert claude_limits.load_session_key(home) == "sk-ant-env"


def test_session_key_absent(home: Path) -> None:
    assert claude_limits.load_session_key(home) is None


def test_refresh_without_key(home: Path) -> None:
    success, note = REAL_REFRESH_FROM_API(home)
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
        if url.endswith("/oauth/profile"):
            # The profile endpoint takes the OAuth token; a session key is
            # refused there with 403 account_session_invalid.
            assert headers["Authorization"] == "Bearer x"
            return FakeResponse(
                200,
                {"organization": {"uuid": ORG_UUID}},
            )
        assert headers["Authorization"] == "Bearer sk-ant-sid01-test"
        if url.endswith("/usage"):
            return FakeResponse(404, {})
        if url.endswith("/rate_limits"):
            return FakeResponse(
                200,
                {
                    "five_hour": {"used_percentage": 40.0, "resets_at": _future_ts(2)},
                    "seven_day": {"used_percentage": 20.0, "resets_at": _future_ts(48)},
                },
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = REAL_REFRESH_FROM_API(home)
    assert success
    assert note is None
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert quotas["5h"].remaining_percent == 60.0
    assert quotas["weekly"].remaining_percent == 80.0


def test_refresh_finds_windows_nested_in_the_usage_payload(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows are located by key, wherever the payload nests them."""
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    def fake_get(url: str, headers: dict, timeout: float) -> FakeResponse:
        if url.endswith("/oauth/profile"):
            return FakeResponse(200, {"organization": {"uuid": ORG_UUID}})
        if url.endswith("/rate_limits"):
            return FakeResponse(403, {})
        if url.endswith("/usage"):
            return FakeResponse(
                200,
                {"data": {"five_hour": {"used_percentage": 55.0}}},
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = REAL_REFRESH_FROM_API(home)
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
            return FakeResponse(200, {"organization": {"uuid": ORG_UUID}})
        return FakeResponse(403, {})

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = REAL_REFRESH_FROM_API(home)
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


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _fake_usage_api(payload: dict):
    """Fake the account-session API, serving `payload` from /usage."""

    def fake_get(url: str, headers: dict, timeout: float) -> _FakeResponse:
        if url.endswith("/oauth/profile"):
            return _FakeResponse(200, {"organization": {"uuid": ORG_UUID}})
        if url.endswith("/usage"):
            return _FakeResponse(200, payload)
        if url.endswith("/rate_limits"):
            # The live endpoint answers 200 with concurrency tiers and no windows.
            return _FakeResponse(200, {"rate_limit_tier": "default_claude_ai"})
        raise AssertionError(f"unexpected url: {url}")

    return fake_get


def _profile_denied_api(payload: dict, seen: list[str]):
    """Fake an API where the profile endpoint accepts no token we hold.

    That is the real state whenever the Claude Code OAuth token has expired:
    the endpoint wants an OAuth token, and the session key is not one.
    """

    def fake_get(url: str, headers: dict, timeout: float) -> _FakeResponse:
        seen.append(url)
        if url.endswith("/oauth/profile"):
            return _FakeResponse(401, {"error": {"type": "authentication_error"}})
        if url.endswith("/usage"):
            return _FakeResponse(200, payload)
        if url.endswith("/rate_limits"):
            return _FakeResponse(200, {"rate_limit_tier": "default_claude_ai"})
        raise AssertionError(f"unexpected url: {url}")

    return fake_get


def test_refresh_uses_the_cached_org_uuid_when_the_profile_is_denied(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh exists for when Claude Code is closed - so it must survive
    that program's OAuth token expiring, which is what closing it leads to."""
    write_profile_cache(home, claude_profile_payload(uuid=CACHED_ORG_UUID))
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    seen: list[str] = []
    monkeypatch.setattr(
        claude_limits.requests,
        "get",
        _profile_denied_api({"five_hour": {"utilization": 44.0}}, seen),
    )
    success, note = REAL_REFRESH_FROM_API(home)
    assert success, note
    assert any(f"{CACHED_ORG_UUID}/usage" in url for url in seen)
    quotas = {q.kind: q.remaining_percent for q in claude.cached_quotas(home)}
    assert quotas["5h"] == 56.0


def test_refresh_reports_the_credential_it_tried(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no cached profile either, the note must name what was attempted.

    Only the OAuth token is named: the session key is never sent to the
    profile endpoint, so reporting it as rejected would blame a working
    cookie for the wrong endpoint's refusal.
    """
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    monkeypatch.setattr(claude_limits.requests, "get", _profile_denied_api({}, []))
    success, note = REAL_REFRESH_FROM_API(home)
    assert not success
    assert "claude code oauth" in (note or "")
    assert "session key" not in (note or "")
    assert "no cached profile" in (note or "")


def test_refresh_parses_the_account_usage_shape(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live /organizations/{uuid}/usage shape: `utilization` plus an ISO
    `resets_at`. Reported as 0 used before this was recognised."""
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    monkeypatch.setattr(
        claude_limits.requests,
        "get",
        _fake_usage_api(
            {
                "five_hour": {
                    "utilization": 32.0,
                    "resets_at": _future_iso(2),
                    "limit_dollars": None,
                },
                "seven_day": {
                    "utilization": 36.0,
                    "resets_at": _future_iso(48),
                    "limit_dollars": None,
                },
                "seven_day_opus": None,
            }
        ),
    )
    success, note = REAL_REFRESH_FROM_API(home)
    assert success
    assert note is None
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert quotas["5h"].remaining_percent == 68.0
    assert quotas["weekly"].remaining_percent == 64.0
    assert quotas["5h"].resets_at is not None


def test_refresh_never_clobbers_a_capture_with_percentless_windows(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window carrying only a reset time says nothing about usage. Accepting
    it used to report a successful refresh that overwrote good statusline data
    with windows rendering as '-'."""
    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    monkeypatch.setattr(
        claude_limits.requests,
        "get",
        _fake_usage_api(
            {
                "five_hour": {"resets_at": "2026-08-20T18:29:59+00:00"},
                "seven_day": {"resets_at": "2026-08-23T11:59:59+00:00"},
            }
        ),
    )
    success, note = REAL_REFRESH_FROM_API(home)
    assert not success
    assert "usage shape unknown" in (note or "")
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert quotas["5h"].remaining_percent == 76.5
    assert quotas["weekly"].remaining_percent == 58.8


def test_tracker_attributes_an_api_refresh_to_the_session(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled refresh runs with Claude Code closed, so the note must not
    credit the statusline capture for numbers the account session fetched."""
    from ai_coding_usage_tracker.tracker import collect_statuses

    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    monkeypatch.setattr(
        claude_limits.requests,
        "get",
        _fake_usage_api({"five_hour": {"utilization": 32.0}}),
    )
    assert REAL_REFRESH_FROM_API(home)[0]
    assert claude_limits.captured_source(home) == claude_limits.SOURCE_ACCOUNT_SESSION
    status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    assert status.quotas_source == claude_limits.SOURCE_ACCOUNT_SESSION
    assert status.quotas_source != claude_limits.SOURCE_STATUSLINE


def test_captured_source_defaults_for_pre_field_snapshots(home: Path) -> None:
    """Snapshots written before the field existed came from the statusline."""
    claude_limits.capture_from_statusline_json(STATUSLINE_PAYLOAD, home)
    target = claude_limits.cache_file(home)
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    del snapshot["source"]
    target.write_text(json.dumps(snapshot), encoding="utf-8")
    assert claude_limits.captured_source(home) == claude_limits.SOURCE_STATUSLINE


def test_refresh_reports_a_failed_cache_write(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh that cannot persist its snapshot has not refreshed anything.

    Reporting success here left `status` showing the previous windows with no
    note explaining why the numbers never moved.
    """
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    monkeypatch.setattr(
        claude_limits.requests,
        "get",
        _fake_usage_api({"five_hour": {"utilization": 30.0}}),
    )
    # Patched here rather than on the shared `fileutil` module, so the test
    # disables only the write it is about.
    monkeypatch.setattr(claude_limits, "_write_snapshot", lambda *_, **__: False)
    success, note = REAL_REFRESH_FROM_API(home)
    assert not success
    assert "could not write" in (note or "")


def test_refresh_survives_a_network_error_on_the_first_credential(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flaky profile lookup must not skip the cached org uuid.

    The cached profile exists precisely for degraded conditions, so aborting
    the whole resolution on the first RequestException threw away the fallback
    that would have worked.
    """
    write_profile_cache(home, claude_profile_payload(uuid=CACHED_ORG_UUID))
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    seen: list[str] = []

    def fake_get(url: str, headers: dict, timeout: float) -> _FakeResponse:
        seen.append(url)
        if url.endswith("/oauth/profile"):
            raise claude_limits.requests.RequestException("connection reset")
        if url.endswith("/usage"):
            return _FakeResponse(200, {"five_hour": {"utilization": 25.0}})
        if url.endswith("/rate_limits"):
            return _FakeResponse(200, {"rate_limit_tier": "default_claude_ai"})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = REAL_REFRESH_FROM_API(home)
    assert success, note
    assert any(f"{CACHED_ORG_UUID}/usage" in url for url in seen)
    quotas = {q.kind: q.remaining_percent for q in claude.cached_quotas(home)}
    assert quotas["5h"] == 75.0


def test_refresh_network_error_note_names_the_credentials_and_the_cause(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no cached profile, the note must survive the fallthrough intact."""
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")

    def fake_get(url: str, headers: dict, timeout: float) -> _FakeResponse:
        raise claude_limits.requests.RequestException("connection reset")

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = REAL_REFRESH_FROM_API(home)
    assert not success
    assert "claude code oauth" in (note or "")
    assert "connection reset" in (note or "")
    assert "no cached profile" in (note or "")


def test_account_level_windows_outrank_a_nested_namesake(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-model `five_hour` must never become the account's headline number.

    The old lookup grabbed the first match from a depth-first walk, so a
    breakdown section nested before the real windows would win silently.
    """
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    monkeypatch.setattr(
        claude_limits.requests,
        "get",
        _fake_usage_api(
            {
                # Sorts first, and nests a same-named window one level down.
                "breakdown_by_model": {
                    "opus": {"five_hour": {"utilization": 99.0}},
                },
                "five_hour": {"utilization": 10.0},
                "seven_day": {"utilization": 20.0},
            }
        ),
    )
    success, note = REAL_REFRESH_FROM_API(home)
    assert success, note
    quotas = {q.kind: q.remaining_percent for q in claude.cached_quotas(home)}
    assert quotas["5h"] == 90.0
    assert quotas["weekly"] == 80.0


def test_windows_are_found_in_a_top_level_list_payload() -> None:
    """A list root must still be walked: the previous lookup accepted one."""
    found = claude_limits._find_windows([{"five_hour": {"utilization": 12.0}}])
    assert found["five_hour"]["utilization"] == 12.0


def test_find_windows_ignores_a_scalar_payload() -> None:
    assert claude_limits._find_windows("nonsense") == {}
    assert claude_limits._find_windows(None) == {}


def _capture_with_reset(
    home: Path,
    used: float,
    resets_in_hours: float,
    source: str = claude_limits.SOURCE_STATUSLINE,
) -> None:
    """Write a fresh snapshot whose 5h window resets at the given offset."""
    claude_limits.capture_windows(
        {"five_hour": {"used_percentage": used, "resets_at": _future_ts(resets_in_hours)}},
        home,
        source=source,
    )


def test_elapsed_window_drops_its_stale_percentage(home: Path) -> None:
    """A percentage from before the reset describes a window that is gone.

    The freshness limit is 6h and the short window is 5h, so a snapshot can
    outlive a whole cycle. Serving the old number told a user with a full
    window that they were nearly exhausted.
    """
    _capture_with_reset(home, used=89.0, resets_in_hours=-3.5)
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert claude_limits.load_cached(home) is not None, "snapshot must still be fresh"
    assert quotas["5h"].remaining_percent is None
    # The reset time survives, so the row can still say the window is resetting.
    assert quotas["5h"].resets_at is not None


def test_unelapsed_window_keeps_its_percentage(home: Path) -> None:
    _capture_with_reset(home, used=89.0, resets_in_hours=2)
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert quotas["5h"].remaining_percent == 11.0


def test_window_without_a_reset_time_keeps_its_percentage(home: Path) -> None:
    """No reset time means nothing is known to have elapsed."""
    claude_limits.capture_windows({"five_hour": {"used_percentage": 89.0}}, home)
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    assert quotas["5h"].remaining_percent == 11.0


def test_elapsed_window_triggers_the_session_refresh(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An elapsed window is missing data, so the refresh must still fire.

    The trigger used to be `not quotas`, which a stale-but-present window
    satisfied - so the one case the session key exists for never refreshed.
    """
    from ai_coding_usage_tracker.tracker import collect_statuses

    _capture_with_reset(home, used=89.0, resets_in_hours=-3.5)
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    # tracker calls the module attribute, which the autouse guard replaces, so
    # the real refresh is restored here - the fake transport below is what
    # keeps it off the network.
    monkeypatch.setattr(claude_limits, "refresh_from_api", REAL_REFRESH_FROM_API)
    monkeypatch.setattr(
        claude_limits.requests,
        "get",
        _fake_usage_api({"five_hour": {"utilization": 4.0, "resets_at": _future_iso(4)}}),
    )
    status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    quotas = {q.kind: q for q in status.quotas}
    assert quotas["5h"].remaining_percent == 96.0
    assert status.quotas_source == claude_limits.SOURCE_ACCOUNT_SESSION


def test_elapsed_window_keeps_reporting_who_wrote_the_snapshot(home: Path) -> None:
    """`quotas_source` is provenance, not a usability flag.

    Suppressing it for an elapsed window made `_fmt_note` fall back to
    SOURCE_STATUSLINE, so a snapshot the account session had written was
    reported as a statusline capture - the exact inference the recorded label
    exists to prevent.
    """
    from ai_coding_usage_tracker.tracker import collect_statuses

    _capture_with_reset(
        home, used=89.0, resets_in_hours=-3.5, source=claude_limits.SOURCE_ACCOUNT_SESSION
    )
    status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    assert status.quotas_source == claude_limits.SOURCE_ACCOUNT_SESSION


def test_cached_and_fresh_paths_agree_on_an_elapsed_window(home: Path) -> None:
    """The overlay used a different predicate from the fresh path, so the same
    snapshot reported a different source depending on cache state."""
    from ai_coding_usage_tracker.tracker import collect_statuses

    _capture_with_reset(
        home, used=89.0, resets_in_hours=-3.5, source=claude_limits.SOURCE_ACCOUNT_SESSION
    )
    fresh = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    # Well inside the cache TTL, so this row comes back through the overlay.
    cached = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    assert "cached" in (cached.note or "")
    assert cached.quotas_source == fresh.quotas_source
    assert cached.quotas_source == claude_limits.SOURCE_ACCOUNT_SESSION


def test_elapsed_window_note_does_not_claim_a_usable_capture(home: Path) -> None:
    """An elapsed window stays in the list with no percentage, which made the
    note branch on a truthy list and claim rate limits it could not show."""
    from ai_coding_usage_tracker.cli import _fmt_note
    from ai_coding_usage_tracker.tracker import collect_statuses

    _capture_with_reset(
        home, used=89.0, resets_in_hours=-3.5, source=claude_limits.SOURCE_ACCOUNT_SESSION
    )
    status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    note = _fmt_note(status)
    assert "stale capture" in note
    assert "rate limits as of" not in note


def test_usable_window_note_names_the_real_source(home: Path) -> None:
    _capture_with_reset(
        home, used=40.0, resets_in_hours=2, source=claude_limits.SOURCE_ACCOUNT_SESSION
    )
    from ai_coding_usage_tracker.cli import _fmt_note
    from ai_coding_usage_tracker.tracker import collect_statuses

    status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    note = _fmt_note(status)
    assert claude_limits.SOURCE_ACCOUNT_SESSION in note
    assert "stale capture" not in note


def test_the_session_key_is_never_sent_to_the_profile_endpoint(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified against the live endpoint: a session key there is answered
    with 403 account_session_invalid, so sending one buys a wasted round trip
    and a note blaming the cookie for the wrong endpoint's refusal."""
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    profile_auth: list[str] = []

    def fake_get(url: str, headers: dict, timeout: float) -> _FakeResponse:
        if url.endswith("/oauth/profile"):
            profile_auth.append(headers["Authorization"])
            return _FakeResponse(200, {"organization": {"uuid": ORG_UUID}})
        if url.endswith("/usage"):
            return _FakeResponse(200, {"five_hour": {"utilization": 5.0}})
        return _FakeResponse(200, {"rate_limit_tier": "default_claude_ai"})

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    assert REAL_REFRESH_FROM_API(home)[0]
    assert profile_auth == ["Bearer x"]


def test_a_network_error_on_usage_still_tries_rate_limits(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failed segment must not decide the whole refresh, and the cause
    has to survive into the note."""
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    seen: list[str] = []

    def fake_get(url: str, headers: dict, timeout: float) -> _FakeResponse:
        seen.append(url)
        if url.endswith("/oauth/profile"):
            return _FakeResponse(200, {"organization": {"uuid": ORG_UUID}})
        if url.endswith("/usage"):
            raise claude_limits.requests.RequestException("connection reset")
        return _FakeResponse(200, {"five_hour": {"used_percentage": 8.0}})

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = REAL_REFRESH_FROM_API(home)
    assert success, note
    assert any(url.endswith("/rate_limits") for url in seen)
    assert {q.kind: q.remaining_percent for q in claude.cached_quotas(home)}["5h"] == 92.0


def test_a_network_error_on_every_segment_reports_the_cause(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")

    def fake_get(url: str, headers: dict, timeout: float) -> _FakeResponse:
        if url.endswith("/oauth/profile"):
            return _FakeResponse(200, {"organization": {"uuid": ORG_UUID}})
        raise claude_limits.requests.RequestException("connection reset")

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    success, note = REAL_REFRESH_FROM_API(home)
    assert not success
    assert "connection reset" in (note or "")


def test_window_containers_offers_each_container_once() -> None:
    """The root and `data` were pre-yielded and then re-walked by the queue."""
    payload = {"data": {"five_hour": {"utilization": 1.0}}, "other": [{"x": 1}]}
    seen = list(claude_limits._window_containers(payload))
    assert sum(1 for c in seen if c is payload) == 1
    assert sum(1 for c in seen if c is payload["data"]) == 1


def test_window_containers_still_prefers_shallower_namesakes() -> None:
    """Switching to a deque must keep the breadth-first ordering intact."""
    payload = {
        "deep": {"deeper": {"five_hour": {"utilization": 99.0}}},
        "shallow": {"five_hour": {"utilization": 10.0}},
    }
    assert claude_limits._find_windows(payload)["five_hour"]["utilization"] == 10.0


def test_org_uuid_accepts_only_canonical_uuids() -> None:
    """The uuid lands in an authenticated request path, so its shape is checked."""
    assert claude_limits._org_uuid({"organization": {"uuid": ORG_UUID}}) == ORG_UUID
    upper = ORG_UUID.upper()
    assert claude_limits._org_uuid({"organization": {"uuid": upper}}) == upper
    for hostile in (
        "../../organizations/someone-else",
        "org-123",
        f"{ORG_UUID}/../admin",
        f"{ORG_UUID}?x=1",
        "",
        None,
        12345,
    ):
        assert claude_limits._org_uuid({"organization": {"uuid": hostile}}) is None
    assert claude_limits._org_uuid({"organization": "not a dict"}) is None
    assert claude_limits._org_uuid("not a payload") is None


def test_refresh_requests_the_uuid_path_verbatim(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed uuid survives percent-encoding unchanged."""
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    seen: list[str] = []
    inner = _fake_usage_api({"five_hour": {"utilization": 10.0}})

    def fake_get(url: str, headers: dict, timeout: float) -> _FakeResponse:
        seen.append(url)
        return inner(url, headers, timeout)

    monkeypatch.setattr(claude_limits.requests, "get", fake_get)
    assert REAL_REFRESH_FROM_API(home)[0]
    assert f"{claude_limits.API_BASE}/organizations/{ORG_UUID}/usage" in seen


def test_refresh_refuses_a_tampered_org_uuid_from_the_profile_cache(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewritten profile cache must not steer the authenticated request.

    The cache is a plain file, and the fallback path reads the uuid straight
    out of it, so a path fragment stored there would otherwise present the
    session key at an endpoint of the writer's choosing.
    """
    write_profile_cache(home, claude_profile_payload(uuid="../../admin/keys"))
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")
    seen: list[str] = []
    monkeypatch.setattr(claude_limits.requests, "get", _profile_denied_api({}, seen))
    success, note = REAL_REFRESH_FROM_API(home)
    assert not success
    assert "no cached profile" in (note or "")
    assert seen and all(url == claude_limits.PROFILE_URL for url in seen)


def test_inherited_session_key_env_is_cleared(home: Path) -> None:
    """No test may inherit a developer's live claude.ai cookie.

    Exporting PLANTRACK_CLAUDE_SESSION_KEY is what the README tells users to
    do, and `load_session_key` reads the environment before the fixture home,
    so an unclean environment used to ship that cookie to api.anthropic.com on
    every `pytest` run.
    """
    inherited = [n for n in os.environ if n.startswith("PLANTRACK_")]
    # The suite pins PLANTRACK_HOME itself; nothing else may survive.
    assert inherited == ["PLANTRACK_HOME"]
    assert claude_limits.load_session_key(home) is None


def test_refresh_is_blocked_from_reaching_the_network(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with a session key in hand, the autouse guard stops the fetch."""
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "sk-ant-sid01-test")

    def explode(*_: object, **__: object) -> object:
        raise AssertionError("the guarded refresh reached the network")

    monkeypatch.setattr(claude_limits.requests, "get", explode)
    assert claude_limits.refresh_from_api is not REAL_REFRESH_FROM_API
    success, note = claude_limits.refresh_from_api(home)
    assert not success
    assert "disabled in tests" in (note or "")
