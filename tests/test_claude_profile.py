"""Tests for the Claude account profile provider."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import REAL_FETCH_PROFILE, claude_profile_payload, write_profile_cache

from ai_coding_usage_tracker.providers import claude_profile
from ai_coding_usage_tracker.tracker import collect_statuses


def test_parse_profile_reads_subscription_facts() -> None:
    info = claude_profile.parse_profile(claude_profile_payload())
    assert info.plan_type == "pro"
    assert info.status == "incomplete"
    assert info.billing == "apple_subscription"
    assert info.email == "user@example.com"
    assert info.trial_ends_at is None


def test_parse_profile_falls_back_to_organization_type() -> None:
    payload = claude_profile_payload(organization_type="claude_team")
    payload["account"]["has_claude_pro"] = False
    info = claude_profile.parse_profile(payload)
    assert info.plan_type == "team"


def test_parse_profile_tolerates_garbage() -> None:
    assert claude_profile.parse_profile(None).plan_type is None
    assert claude_profile.parse_profile({"organization": "nope"}).status is None


def test_trial_end_is_parsed_as_aware_utc() -> None:
    payload = claude_profile_payload(
        claude_code_trial_ends_at="2026-09-01T00:00:00.000000Z"
    )
    trial = claude_profile.parse_profile(payload).trial_ends_at
    assert trial is not None and trial.tzinfo is not None


def test_app_store_incomplete_is_not_concerning() -> None:
    """Apple/Google billing leaves Anthropic's own record permanently open."""
    assert not claude_profile.status_is_concerning("incomplete", "apple_subscription")
    assert not claude_profile.status_is_concerning("canceled", "apple_subscription")
    assert not claude_profile.status_is_concerning("active", "stripe")


def test_direct_billing_problem_is_concerning() -> None:
    assert claude_profile.status_is_concerning("past_due", "stripe")
    assert claude_profile.status_is_concerning("Canceled", None)


def test_fresh_cache_is_used_without_fetching(home: Path) -> None:
    write_profile_cache(home, claude_profile_payload())
    profile, note = claude_profile.load_profile(home)
    assert note is None
    assert claude_profile.parse_profile(profile).plan_type == "pro"


def test_stale_cache_is_served_when_the_fetch_fails(home: Path) -> None:
    old = datetime.now(tz=timezone.utc) - timedelta(days=3)
    write_profile_cache(home, claude_profile_payload(), fetched_at=old)
    assert claude_profile.load_cached(home) is None
    profile, note = claude_profile.load_profile(home)
    assert profile is not None
    assert note is not None and "last known" in note


def test_missing_cache_and_failed_fetch_report_the_reason(home: Path) -> None:
    profile, note = claude_profile.load_profile(home)
    assert profile is None
    assert note == "profile fetch disabled in tests"


def test_fetch_prefers_the_oauth_token(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return claude_profile_payload()

    def fake_get(url: str, headers: dict, timeout: float) -> FakeResponse:
        calls.append(headers["Authorization"])
        return FakeResponse()

    monkeypatch.setattr(claude_profile.requests, "get", fake_get)
    profile, note = REAL_FETCH_PROFILE(home)
    assert note is None
    assert calls == ["Bearer x"]  # the OAuth access token from the fixture home
    # The successful fetch is cached for the next run.
    assert claude_profile.load_cached(home) == profile


def test_fetch_without_credentials(tmp_path: Path) -> None:
    profile, note = REAL_FETCH_PROFILE(tmp_path)
    assert profile is None
    assert note == "no Claude credentials found"


def test_status_note_flags_a_real_billing_problem(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTRACK_HOME", str(home))
    write_profile_cache(
        home,
        claude_profile_payload(billing_type="stripe", subscription_status="past_due"),
    )
    claude_status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    assert claude_status.subscription is not None
    assert claude_status.subscription.status == "past_due"
    assert "subscription past_due" in (claude_status.note or "")


def test_status_note_stays_quiet_for_app_store_billing(home: Path) -> None:
    write_profile_cache(home, claude_profile_payload())
    claude_status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    assert "incomplete" not in (claude_status.note or "")
