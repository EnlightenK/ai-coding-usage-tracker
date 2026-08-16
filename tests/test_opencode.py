"""Tests for the OpenCode local usage parser."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from ai_coding_usage_tracker.providers import opencode


def test_usage_attributes_zai_plan(home: Path) -> None:
    records = list(opencode.iter_usage(home))
    assert len(records) == 1
    record = records[0]
    assert record.plan_id == "glm-intl"
    assert record.model == "glm-5.2"
    assert record.source == "opencode"
    assert record.input_tokens == 500
    assert record.output_tokens == 80
    assert record.reasoning_tokens == 10
    assert record.cache_read_tokens == 40
    assert record.cache_write_tokens == 4


def test_unknown_providers_skipped(home: Path) -> None:
    records = list(opencode.iter_usage(home))
    assert all(r.plan_id == "glm-intl" for r in records)


def test_since_filter(home: Path) -> None:
    since = date.today() + timedelta(days=1)
    assert list(opencode.iter_usage(home, since=since)) == []


def test_missing_storage_returns_nothing(tmp_path: Path) -> None:
    assert list(opencode.iter_usage(tmp_path)) == []
