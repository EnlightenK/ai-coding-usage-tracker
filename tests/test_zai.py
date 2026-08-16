"""Tests for the Z.ai monitor quota provider."""

from __future__ import annotations

import pytest
import requests
from conftest import REAL_FETCH_LIMITS

from ai_coding_usage_tracker.providers import zai

SUCCESS_PAYLOAD = {
    "code": 200,
    "msg": "Operation successful",
    "success": True,
    "data": {
        "limits": [
            {
                "type": "TOKENS_LIMIT",
                "unit": 3,
                "number": 5,
                "percentage": 49,
                "nextResetTime": 1786871063472,
            },
            {
                "type": "TOKENS_LIMIT",
                "unit": 6,
                "number": 1,
                "percentage": 27,
                "nextResetTime": 1786900149997,
            },
            {
                "type": "TIME_LIMIT",
                "unit": 5,
                "number": 1,
                "usage": 1000,
                "currentValue": 0,
                "remaining": 1000,
                "percentage": 0,
            },
        ],
        "level": "pro",
    },
}


def test_parse_success() -> None:
    result = zai.parse_limits(SUCCESS_PAYLOAD)
    assert result.active is True
    quotas = {q.kind: q for q in result.quotas}
    assert set(quotas) == {"5h", "weekly"}
    assert quotas["5h"].remaining_percent == 51.0
    assert quotas["weekly"].remaining_percent == 73.0
    assert quotas["5h"].resets_at is not None
    assert result.subscription is not None
    assert result.subscription.plan_type == "pro"
    assert result.subscription.valid_until is None


def test_parse_unsuccessful() -> None:
    result = zai.parse_limits({"success": False, "msg": "boom"})
    assert result.active is None
    assert "boom" in (result.note or "")


def test_parse_no_token_limits() -> None:
    payload = {"success": True, "data": {"limits": [{"type": "TIME_LIMIT"}], "level": "lite"}}
    result = zai.parse_limits(payload)
    assert result.active is None
    assert result.subscription is not None
    assert result.subscription.plan_type == "lite"


def test_fetch_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(zai.requests, "get", boom)
    result = REAL_FETCH_LIMITS("key")
    assert result.active is None
    assert "network error" in (result.note or "")


def test_fetch_auth_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 401

        def json(self) -> dict:
            return {}

    monkeypatch.setattr(zai.requests, "get", lambda *a, **k: FakeResponse())
    result = REAL_FETCH_LIMITS("bad-key")
    assert result.active is False
    assert "authentication" in (result.note or "")


def test_fetch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return SUCCESS_PAYLOAD

    captured: dict = {}

    def fake_get(url: str, headers: dict, timeout: float) -> FakeResponse:
        captured["url"] = url
        captured["auth"] = headers["Authorization"]
        return FakeResponse()

    monkeypatch.setattr(zai.requests, "get", fake_get)
    result = REAL_FETCH_LIMITS("raw-key")
    assert result.active is True
    assert captured["url"] == zai.QUOTA_LIMIT_URL
    assert captured["auth"] == "raw-key"
    assert len(result.quotas) == 2
