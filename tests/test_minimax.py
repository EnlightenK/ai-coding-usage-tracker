"""Tests for the MiniMax token plan remains parser and fetcher."""

from __future__ import annotations

import pytest
import requests
from conftest import REAL_FETCH_REMAINS

from ai_coding_usage_tracker.providers import minimax

SUCCESS_PAYLOAD = {
    "model_remains": [
        {
            "start_time": 1786863600000,
            "end_time": 1786881600000,
            "model_name": "general",
            "current_interval_remaining_percent": 92,
            "current_weekly_remaining_percent": 78,
            "weekly_end_time": 1786896000000,
        },
        {
            "model_name": "video",
            "current_interval_remaining_percent": 100,
            "current_weekly_remaining_percent": 100,
        },
    ],
    "base_resp": {"status_code": 0, "status_msg": "success"},
}


def test_parse_success_payload() -> None:
    result = minimax.parse_remains(SUCCESS_PAYLOAD)
    assert result.active is True
    assert [q.kind for q in result.quotas] == ["5h", "weekly"]
    five_hour, weekly = result.quotas
    assert five_hour.remaining_percent == 92
    assert five_hour.resets_at is not None
    assert weekly.remaining_percent == 78


def test_parse_no_subscription() -> None:
    payload = {
        "model_remains": None,
        "base_resp": {"status_code": 2062, "status_msg": "no active token plan subscription"},
    }
    result = minimax.parse_remains(payload)
    assert result.active is False
    assert "2062" in (result.note or "")


def test_parse_unknown_error_is_not_active() -> None:
    """Only 2062 is known to mean 'no subscription'; other errors are unknown."""
    payload = {
        "model_remains": None,
        "base_resp": {"status_code": 1004, "status_msg": "invalid api key"},
    }
    result = minimax.parse_remains(payload)
    assert result.active is None
    assert "1004" in (result.note or "")


def test_parse_empty_list() -> None:
    result = minimax.parse_remains(
        {"model_remains": [], "base_resp": {"status_code": 0}}
    )
    assert result.active is None


def test_fetch_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(minimax.requests, "get", boom)
    result = REAL_FETCH_REMAINS("key", "https://www.minimaxi.com")
    assert result.active is None
    assert "network error" in (result.note or "")


def test_fetch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return SUCCESS_PAYLOAD

    monkeypatch.setattr(
        minimax.requests, "get", lambda *a, **k: FakeResponse()
    )
    result = REAL_FETCH_REMAINS("key", "https://www.minimaxi.com")
    assert result.active is True
    assert len(result.quotas) == 2


def test_fetch_refuses_untrusted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-MiniMax host must never receive the Bearer key."""

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("requests.get must not be called for an untrusted host")

    monkeypatch.setattr(minimax.requests, "get", boom)
    result = REAL_FETCH_REMAINS("key", "http://evil.example")
    assert result.active is None
    assert "untrusted host" in (result.note or "")
    assert result.quotas == []
