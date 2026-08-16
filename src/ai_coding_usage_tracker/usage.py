"""Aggregation of usage records across all local coding tool logs."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import paths
from .config import disabled_plans
from .models import UsageRecord
from .providers import claude, codex, opencode

COUNTER_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "requests",
)


def iter_all_usage(home: Path | None = None, since: date | None = None) -> Iterator[UsageRecord]:
    """Yield raw usage records from Claude Code, Codex and OpenCode logs."""
    yield from claude.iter_usage(home, since)
    yield from codex.iter_usage(home, since)
    yield from opencode.iter_usage(home, since)


def aggregate(records: Iterable[UsageRecord]) -> list[UsageRecord]:
    """Merge raw records into one row per (date, source, plan, model)."""
    totals: dict[tuple[date, str, str, str], UsageRecord] = {}
    for record in records:
        key = (record.date, record.source, record.plan_id, record.model)
        current = totals.get(key)
        if current is None:
            totals[key] = record
            continue
        totals[key] = replace(
            current,
            **{field: getattr(current, field) + getattr(record, field) for field in COUNTER_FIELDS},
        )
    return sorted(
        totals.values(),
        key=lambda r: (r.date, r.plan_id, r.source, r.model),
        reverse=True,
    )


def collect_usage(home: Path | None = None, days: int = 14) -> list[UsageRecord]:
    """Return aggregated daily usage for the last `days` days."""
    home = home or paths.default_home()
    # Records are dated in UTC, so the window must start from a UTC today.
    since = datetime.now(tz=timezone.utc).date() - timedelta(days=days - 1)
    disabled = disabled_plans(home)
    records = [r for r in iter_all_usage(home, since) if r.plan_id not in disabled]
    return aggregate(records)
