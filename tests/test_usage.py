"""Tests for usage aggregation across providers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ai_coding_usage_tracker.models import UsageRecord
from ai_coding_usage_tracker.usage import aggregate, collect_usage


def _record(day: date, source: str, plan: str, model: str, **tokens: int) -> UsageRecord:
    return UsageRecord(
        date=day, source=source, plan_id=plan, model=model, **tokens
    )


def test_aggregate_merges_same_key() -> None:
    day = date(2026, 8, 15)
    merged = aggregate(
        [
            _record(day, "opencode", "glm-intl", "glm-5.2", input_tokens=10, requests=1),
            _record(
                day, "opencode", "glm-intl", "glm-5.2", input_tokens=5, output_tokens=2, requests=1
            ),
        ]
    )
    assert len(merged) == 1
    assert merged[0].input_tokens == 15
    assert merged[0].output_tokens == 2
    assert merged[0].requests == 2
    assert merged[0].total_tokens == 17


def test_aggregate_keeps_distinct_keys_sorted_desc() -> None:
    older = date(2026, 8, 14)
    newer = date(2026, 8, 15)
    merged = aggregate(
        [
            _record(older, "codex", "chatgpt-codex", "codex", input_tokens=1),
            _record(newer, "claude-code", "claude-code", "claude-sonnet-4-5", input_tokens=2),
        ]
    )
    assert merged[0].date == newer
    assert merged[1].date == older


def test_collect_usage_reads_all_sources(home: Path) -> None:
    records = collect_usage(home, days=3650)
    sources = {r.source for r in records}
    assert sources == {"claude-code", "codex", "opencode"}
    plans = {r.plan_id for r in records}
    assert plans == {"claude-code", "glm-intl", "minimax-cn", "chatgpt-codex"}
