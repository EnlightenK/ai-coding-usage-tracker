"""Tests for the SQLite store: usage history, snapshots and the status cache."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_coding_usage_tracker import store
from ai_coding_usage_tracker.models import PlanStatus, QuotaWindow, SubscriptionInfo, UsageRecord


def _usage(day: str, plan: str, source: str = "claude-code", model: str = "m", **tokens: int):
    return UsageRecord(
        date=datetime.fromisoformat(day).date(),
        source=source,
        plan_id=plan,
        model=model,
        **tokens,
    )


def _recent_day() -> str:
    """A day inside every window these tests read back through."""
    return (datetime.now(tz=timezone.utc).date() - timedelta(days=1)).isoformat()


def test_usage_record_roundtrip(tmp_path: Path) -> None:
    # Relative for the same reason as test_usage_history_filters_days_and_plan
    # below: a hardcoded date ages out of the days=30 window read back here.
    records = [_usage(_recent_day(), "glm-intl", input_tokens=10, output_tokens=2, requests=1)]
    assert store.record_usage(tmp_path, records) == 1
    history = store.usage_history(tmp_path, days=30)
    assert len(history) == 1
    assert history[0].input_tokens == 10
    assert history[0].requests == 1


def test_usage_upsert_replaces_instead_of_adding(tmp_path: Path) -> None:
    """Re-parsing the same logs must not double the recorded totals."""
    day = _recent_day()
    store.record_usage(tmp_path, [_usage(day, "glm-intl", input_tokens=10)])
    store.record_usage(tmp_path, [_usage(day, "glm-intl", input_tokens=25)])
    history = store.usage_history(tmp_path, days=30)
    assert len(history) == 1
    assert history[0].input_tokens == 25


def test_usage_history_filters_days_and_plan(tmp_path: Path) -> None:
    # Dates are relative to today: a `days=3` window over hardcoded dates stops
    # matching as soon as the wall clock moves past them.
    today = datetime.now(tz=timezone.utc).date()
    inside = (today - timedelta(days=1)).isoformat()
    outside = (today - timedelta(days=20)).isoformat()
    store.record_usage(
        tmp_path,
        [
            _usage(outside, "glm-intl", input_tokens=1),
            _usage(inside, "glm-intl", input_tokens=2),
            _usage(inside, "minimax-cn", input_tokens=3),
        ],
    )
    by_plan = store.usage_history(tmp_path, days=3, plan_id="glm-intl")
    assert [r.input_tokens for r in by_plan] == [2]
    recent = store.usage_history(tmp_path, days=3)
    assert {r.input_tokens for r in recent} == {2, 3}  # the older row is outside
    older = store.usage_history(tmp_path, days=30)
    assert {r.input_tokens for r in older} == {1, 2, 3}


def test_record_usage_empty_is_noop(tmp_path: Path) -> None:
    assert store.record_usage(tmp_path, []) == 0
    assert store.usage_history(tmp_path, days=30) == []


def _plan_status(plan_id: str, active=True, quotas=(), plan_type="pro", note="ok"):
    return PlanStatus(
        plan_id=plan_id,
        provider="test",
        name="Test Plan",
        region=None,
        auth_kind="api-key",
        configured=True,
        active=active,
        subscription=SubscriptionInfo(
            plan_type=plan_type,
            valid_until=None,
            days_left=None,
            status=None,
            billing=None,
        ),
        quotas=list(quotas),
        note=note,
    )


def test_status_snapshot_roundtrip(tmp_path: Path) -> None:
    resets = datetime.now(tz=timezone.utc) + timedelta(hours=3)
    statuses = [
        _plan_status(
            "glm-intl",
            quotas=[QuotaWindow(kind="5h", remaining_percent=51.0, resets_at=resets)],
        )
    ]
    store.record_status(tmp_path, statuses)
    history = store.status_history(tmp_path, hours=1)
    assert len(history) == 1
    row = history[0]
    assert row["plan_id"] == "glm-intl"
    assert row["active"] is True
    assert row["plan_type"] == "pro"
    assert row["quotas"][0].kind == "5h"
    assert row["quotas"][0].remaining_percent == 51.0
    assert row["quotas"][0].resets_at == resets


def test_status_history_excludes_old_snapshots(tmp_path: Path) -> None:
    store.record_status(tmp_path, [_plan_status("glm-intl")])
    assert store.status_history(tmp_path, hours=1)  # sanity: fresh snapshot visible
    # Manually age the row beyond the query window.
    with sqlite3.connect(store.db_path(tmp_path)) as conn:
        old = (datetime.now(tz=timezone.utc) - timedelta(hours=5)).isoformat()
        conn.execute("UPDATE status_snapshots SET captured_at = ?", (old,))
    assert store.status_history(tmp_path, hours=1, plan_id="glm-intl") == []


def test_status_cache_fresh_and_expired(tmp_path: Path) -> None:
    store.record_status(tmp_path, [_plan_status("glm-intl", note="hello")])
    hit = store.cached_status(tmp_path, "glm-intl", max_age_seconds=300)
    assert hit is not None
    payload, stored_at = hit
    assert payload["plan_id"] == "glm-intl"
    assert payload["note"] == "hello"
    assert stored_at.tzinfo is not None
    assert store.cached_status(tmp_path, "glm-intl", max_age_seconds=0) is None
    assert store.cached_status(tmp_path, "unknown-plan", max_age_seconds=300) is None


def test_snapshot_cache_roundtrip_preserves_datetimes(tmp_path: Path) -> None:
    resets = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    store.record_status(
        tmp_path,
        [
            _plan_status(
                "glm-intl",
                quotas=[QuotaWindow(kind="weekly", remaining_percent=70.0, resets_at=resets)],
            )
        ],
    )
    payload, _ = store.cached_status(tmp_path, "glm-intl", max_age_seconds=300)
    # json.dumps(default=str) renders datetimes with a space separator; they
    # must still round-trip to the same instant via the shared ISO parser.
    raw = payload["quotas"][0]["resets_at"]
    assert datetime.fromisoformat(raw) == resets
