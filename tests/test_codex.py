"""Tests for the Codex provider."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from conftest import (
    CODEX_ACTIVE_UNTIL,
    CODEX_SESSION_DIR,
    FIXTURE_DAY,
    fixture_time,
    write_lines,
)

from ai_coding_usage_tracker.providers import codex


def test_decode_jwt_payload_roundtrip() -> None:
    token = codex.decode_jwt_payload.__name__
    assert token
    claims = {"email": "a@b.c", "n": 1}
    import base64
    import json

    header = base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    decoded = codex.decode_jwt_payload(f"{header}.{payload}.sig")
    assert decoded == claims


def test_subscription_status(home: Path) -> None:
    sub = codex.subscription_status(home)
    assert sub is not None
    assert sub.plan_type == "plus"
    assert sub.email == "user@example.com"
    assert sub.valid_until is not None
    # Compared against the fixture's own claim: asserting a written-in year
    # pins the test to whichever day the fixture happened to be authored on.
    assert sub.valid_until == datetime.fromisoformat(CODEX_ACTIVE_UNTIL)


def test_usage_aggregates_cumulative_session_totals(home: Path) -> None:
    records = list(codex.iter_usage(home))
    assert len(records) == 1
    record = records[0]
    assert record.plan_id == "chatgpt-codex"
    assert record.source == "codex"
    assert record.input_tokens == 30
    assert record.output_tokens == 15
    assert record.cache_read_tokens == 300
    assert record.reasoning_tokens == 8
    assert record.requests == 2


def test_usage_since_filter_excludes_old(home: Path) -> None:
    since = date.today() + timedelta(days=1)
    assert list(codex.iter_usage(home, since=since)) == []


def test_minimax_provider_session_attributed(home: Path, tmp_path: Path) -> None:
    session = home / ".codex" / "sessions" / CODEX_SESSION_DIR / "rollout-mm.jsonl"
    write_lines(
        session,
        [
            {
                "timestamp": fixture_time(20),
                "type": "session_meta",
                "payload": {"model_provider": "minimax"},
            },
            {
                "timestamp": fixture_time(20, 1),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 5, "output_tokens": 5}},
                },
            },
        ],
    )
    records = [r for r in codex.iter_usage(home) if r.input_tokens == 5]
    assert records[0].plan_id == "minimax-intl"


def test_total_only_session_counts_tokens(home: Path) -> None:
    session = home / ".codex" / "sessions" / CODEX_SESSION_DIR / "rollout-total.jsonl"
    write_lines(
        session,
        [
            {
                "timestamp": fixture_time(21),
                "type": "session_meta",
                "payload": {"model_provider": "openai"},
            },
            {
                "timestamp": fixture_time(21, 1),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 29950,
                        }
                    },
                },
            },
        ],
    )
    records = [r for r in codex.iter_usage(home) if r.date == FIXTURE_DAY and r.source == "codex"]
    total_only = [r for r in records if r.requests == 1 and r.output_tokens == 0]
    assert total_only[0].input_tokens == 29950
