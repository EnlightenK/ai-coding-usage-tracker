"""Shared helpers for reading JSON files and normalizing timestamps and tokens."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def read_json(path: Path) -> object | None:
    """Load any JSON value from disk, returning None for anything unusable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_json_dict(path: Path) -> dict | None:
    """Load a JSON object from disk, returning None for anything unusable."""
    data = read_json(path)
    return data if isinstance(data, dict) else None


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 string; naive timestamps are treated as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def ms_to_datetime(value: object) -> datetime | None:
    """Convert an epoch-milliseconds number to an aware UTC datetime.

    Hostile epoch values (for example absurdly large numbers) raise
    OverflowError/OSError from ``fromtimestamp``; those map to None instead
    of crashing the caller.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def positive_int(value: object) -> int:
    """Keep a positive JSON integer; everything else maps to 0."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


def now_ms() -> int:
    """Current UTC time as epoch milliseconds."""
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)
