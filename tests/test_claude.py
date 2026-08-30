"""Tests for the Claude Code provider."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from conftest import claude_profile_payload, write_profile_cache

from ai_coding_usage_tracker.providers import claude, claude_limits


def test_subscription_status_without_profile(home: Path) -> None:
    """Offline: tier still comes from local credentials, but no fake countdown."""
    sub = claude.subscription_status(home)
    assert sub is not None
    assert sub.plan_type == "pro"
    assert sub.source == "local credentials"
    # The OAuth lifetime is auth state, never a subscription end date.
    assert sub.valid_until is None
    assert sub.days_left is None
    assert sub.auth_valid_until is not None
    assert sub.auth_valid_until.tzinfo is not None
    assert 24 < (sub.auth_days_left or 0) < 26


def test_subscription_status_from_profile(home: Path) -> None:
    write_profile_cache(home, claude_profile_payload())
    sub = claude.subscription_status(home)
    assert sub is not None
    assert sub.plan_type == "pro"
    assert sub.status == "incomplete"
    assert sub.billing == "apple_subscription"
    assert sub.email == "user@example.com"
    assert sub.source == "claude.ai profile"
    assert sub.days_left is None


def test_subscription_status_reports_max_plan(home: Path) -> None:
    payload = claude_profile_payload(organization_type="claude_max")
    payload["account"]["has_claude_max"] = True
    write_profile_cache(home, payload)
    sub = claude.subscription_status(home)
    assert sub is not None and sub.plan_type == "max"


def test_subscription_status_counts_down_a_trial(home: Path) -> None:
    ends = datetime.now(tz=timezone.utc) + timedelta(days=9)
    write_profile_cache(home, claude_profile_payload(claude_code_trial_ends_at=ends.isoformat()))
    sub = claude.subscription_status(home)
    assert sub is not None
    assert sub.valid_until is not None
    assert 8 < (sub.days_left or 0) < 10


def test_usage_parses_and_dedupes(home: Path) -> None:
    records = list(claude.iter_usage(home))
    assert len(records) == 3
    by_model = {r.model: r for r in records}
    sonnet = by_model["claude-sonnet-4-5"]
    assert sonnet.input_tokens == 100
    assert sonnet.output_tokens == 50
    assert sonnet.cache_read_tokens == 1000
    assert sonnet.cache_write_tokens == 200
    assert sonnet.requests == 1


def test_usage_attributes_plans_by_model(home: Path) -> None:
    records = list(claude.iter_usage(home))
    plans = {r.model: r.plan_id for r in records}
    assert plans["claude-sonnet-4-5"] == "claude-code"
    assert plans["glm-5.2"] == "glm-intl"
    assert plans["MiniMax-M3"] == "minimax-cn"


def test_usage_since_filter(home: Path) -> None:
    since = date.today() + timedelta(days=1)
    assert list(claude.iter_usage(home, since=since)) == []
    assert list(claude.iter_usage(home)) != []


def test_missing_home_returns_nothing(tmp_path: Path) -> None:
    assert list(claude.iter_usage(tmp_path)) == []
    assert claude.subscription_status(tmp_path) is None


def test_cached_quotas_tolerates_hostile_reset_epoch(home: Path) -> None:
    """An absurd resets_at must degrade to None, not crash the status run."""
    assert claude_limits.capture_windows(
        {"five_hour": {"used_percentage": 40.0, "resets_at": 1e30}}, home
    )
    quotas = {quota.kind: quota for quota in claude.cached_quotas(home)}
    assert quotas["5h"].remaining_percent == 60.0
    assert quotas["5h"].resets_at is None
