"""Tests for the shared parsing helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from ai_coding_usage_tracker.parsing import ms_to_datetime, parse_iso


def test_ms_to_datetime_converts_valid_milliseconds() -> None:
    result = ms_to_datetime(1_786_900_000_000)
    assert result == datetime(2026, 8, 16, 17, 6, 40, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc


def test_ms_to_datetime_tolerates_hostile_epoch_values() -> None:
    # An absurd magnitude would raise OverflowError/OSError from
    # fromtimestamp; it must map to None instead of crashing the caller.
    assert ms_to_datetime(1e30) is None
    assert ms_to_datetime(float("inf")) is None


def test_ms_to_datetime_rejects_non_positive_and_non_numeric() -> None:
    assert ms_to_datetime(-1) is None
    assert ms_to_datetime(0) is None
    assert ms_to_datetime(None) is None
    assert ms_to_datetime("123") is None
    # Booleans are ints structurally, but are never valid epochs here.
    assert ms_to_datetime(True) is None
    assert ms_to_datetime(False) is None


def test_parse_iso_accepts_z_suffix() -> None:
    assert parse_iso("2026-08-15T10:00:00.000Z") == datetime(
        2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc
    )


def test_parse_iso_treats_naive_values_as_utc() -> None:
    assert parse_iso("2026-08-15T10:00:00") == datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_preserves_explicit_offset() -> None:
    assert parse_iso("2026-08-15T10:00:00+02:00") == datetime(
        2026, 8, 15, 8, 0, 0, tzinfo=timezone.utc
    )


def test_parse_iso_rejects_garbage_and_non_strings() -> None:
    assert parse_iso("not-a-timestamp") is None
    assert parse_iso("") is None
    assert parse_iso(None) is None
    assert parse_iso(123) is None
