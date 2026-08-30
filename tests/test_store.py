"""Tests for the SQLite store: usage history, snapshots and the status cache."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


def test_usage_record_roundtrip(tmp_path: Path) -> None:
    records = [_usage("2026-08-15", "glm-intl", input_tokens=10, output_tokens=2, requests=1)]
    assert store.record_usage(tmp_path, records) == 1
    history = store.usage_history(tmp_path, days=30)
    assert len(history) == 1
    assert history[0].input_tokens == 10
    assert history[0].requests == 1


def test_usage_upsert_replaces_instead_of_adding(tmp_path: Path) -> None:
    """Re-parsing the same logs must not double the recorded totals."""
    store.record_usage(tmp_path, [_usage("2026-08-15", "glm-intl", input_tokens=10)])
    store.record_usage(tmp_path, [_usage("2026-08-15", "glm-intl", input_tokens=25)])
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


def _codex_account(day: str):
    return _usage(
        day,
        "chatgpt-codex",
        source="codex-account",
        model="chatgpt-codex account total",
        input_tokens=1_000_000,
    )


def _codex_local(day: str):
    return _usage(
        day,
        "chatgpt-codex",
        source="codex",
        model="gpt-5-codex",
        input_tokens=600_000,
        output_tokens=400_000,
        requests=7,
    )


def _tokens(records: list[UsageRecord]) -> int:
    return sum(r.input_tokens + r.output_tokens for r in records)


def test_codex_local_sessions_supersede_account_totals(tmp_path: Path) -> None:
    """The two Codex channels describe the same usage, so only one may survive.

    `codex.iter_usage` reports a day either as an account-wide total or as
    locally parsed sessions, depending on whether the Codex CLI answered; both
    rows surviving would make `usage_history` count that day twice.
    """
    day = datetime.now(tz=timezone.utc).date().isoformat()
    store.record_usage(tmp_path, [_codex_account(day)])
    store.record_usage(tmp_path, [_codex_local(day)])
    history = store.usage_history(tmp_path, days=2, plan_id="chatgpt-codex")
    assert [r.source for r in history] == ["codex"]
    assert _tokens(history) == 1_000_000
    assert history[0].requests == 7


def test_codex_account_totals_supersede_local_sessions(tmp_path: Path) -> None:
    """The reverse order converges on the newer channel just the same."""
    day = datetime.now(tz=timezone.utc).date().isoformat()
    store.record_usage(tmp_path, [_codex_local(day)])
    store.record_usage(tmp_path, [_codex_account(day)])
    history = store.usage_history(tmp_path, days=2, plan_id="chatgpt-codex")
    assert [r.source for r in history] == ["codex-account"]
    assert _tokens(history) == 1_000_000


def test_codex_supersede_is_scoped_to_batch_days_and_plan(tmp_path: Path) -> None:
    """Superseding one day's Codex channel must not disturb anything else."""
    today = datetime.now(tz=timezone.utc).date()
    day, older = today.isoformat(), (today - timedelta(days=1)).isoformat()
    store.record_usage(
        tmp_path,
        [
            _codex_local(day),
            _codex_local(older),
            # Codex sessions billed to another plan, plus unrelated sources.
            _usage(day, "minimax-intl", source="codex", model="MiniMax-M3", input_tokens=5),
            _usage(day, "claude-code", source="claude-code", input_tokens=6),
            _usage(day, "glm-intl", source="opencode", input_tokens=7),
        ],
    )
    store.record_usage(tmp_path, [_codex_account(day)])
    kept = {
        (r.date.isoformat(), r.plan_id, r.source): r.input_tokens
        for r in store.usage_history(tmp_path, days=3)
    }
    assert kept == {
        (day, "chatgpt-codex", "codex-account"): 1_000_000,
        (older, "chatgpt-codex", "codex"): 600_000,
        (day, "minimax-intl", "codex"): 5,
        (day, "claude-code", "claude-code"): 6,
        (day, "glm-intl", "opencode"): 7,
    }


def test_schema_is_applied_once_per_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads and later writes reuse the schema the first connection created."""
    scripts: list[str] = []
    real_connect = sqlite3.connect

    class CountingConnection(sqlite3.Connection):
        def executescript(self, script: str):  # type: ignore[override]
            scripts.append(script)
            return super().executescript(script)

    monkeypatch.setattr(
        store.sqlite3,
        "connect",
        lambda *args, **kwargs: real_connect(*args, factory=CountingConnection, **kwargs),
    )
    store.record_usage(tmp_path, [_usage("2026-08-15", "glm-intl", input_tokens=10)])
    store.usage_history(tmp_path, days=30)
    store.cached_status(tmp_path, "glm-intl", max_age_seconds=300)
    assert len(scripts) == 1


def test_schema_is_recreated_after_the_database_is_removed(tmp_path: Path) -> None:
    """A database deleted between calls is rebuilt, cache of applied paths aside."""
    store.record_usage(tmp_path, [_usage("2026-08-15", "glm-intl", input_tokens=10)])
    store.db_path(tmp_path).unlink()
    assert store.usage_history(tmp_path, days=30) == []
    assert store.record_usage(tmp_path, [_usage("2026-08-15", "glm-intl", input_tokens=4)]) == 1
    assert [r.input_tokens for r in store.usage_history(tmp_path, days=30)] == [4]


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
