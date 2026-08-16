"""Tests for parallel status collection and its SQLite-backed cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding_usage_tracker.models import QuotaWindow
from ai_coding_usage_tracker.providers import zai
from ai_coding_usage_tracker.tracker import collect_statuses


@pytest.fixture
def counting_zai(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch zai.fetch_limits with a call counter (overrides the autouse guard)."""
    calls: list[int] = []

    def fake_limits(api_key: str, timeout: float = 15.0) -> zai.ZaiQuota:
        calls.append(1)
        return zai.ZaiQuota(
            active=True,
            quotas=[QuotaWindow(kind="5h", remaining_percent=55.0, resets_at=None)],
        )

    monkeypatch.setattr(zai, "fetch_limits", fake_limits)
    return calls


def _glm(statuses):
    return next(s for s in statuses if s.plan_id == "glm-intl")


def test_second_run_is_served_from_cache(home: Path, counting_zai: list[int]) -> None:
    first = collect_statuses(home)
    assert len(counting_zai) == 1
    assert _glm(first).active is True
    second = collect_statuses(home)
    assert len(counting_zai) == 1  # no refetch inside the TTL
    glm = _glm(second)
    assert "cached" in (glm.note or "")
    assert glm.active is True
    quotas = {q.kind: q for q in glm.quotas}
    assert quotas["5h"].remaining_percent == 55.0


def test_refresh_bypasses_the_cache(home: Path, counting_zai: list[int]) -> None:
    collect_statuses(home)
    collect_statuses(home, refresh=True)
    assert len(counting_zai) == 2
    assert "cached" not in (_glm(collect_statuses(home, refresh=True)).note or "")


def test_zero_ttl_disables_cache_reads(home: Path, counting_zai: list[int]) -> None:
    collect_statuses(home)
    collect_statuses(home, max_cache_age=0)
    assert len(counting_zai) == 2
