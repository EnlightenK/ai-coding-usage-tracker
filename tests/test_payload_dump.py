"""Tests for optional raw-payload dumping (PLANTRACK_DEBUG_PAYLOAD)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_coding_usage_tracker import payload_dump
from ai_coding_usage_tracker.providers import minimax, zai


def test_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(payload_dump.DUMP_ENV, raising=False)
    assert not payload_dump.dump("x", {"a": 1}, tmp_path)
    assert not payload_dump.dump_dir(tmp_path).exists()


_ENV_CASES = [
    ("1", True),
    ("true", True),
    ("YES", True),
    ("on", True),
    ("  1  ", True),
    ("0", False),
    ("false", False),
    ("no", False),
    # Dumping is opt-in, so anything that is not an explicit "on" leaves it off.
    # These three used to ENABLE it, quietly accumulating account emails and
    # plan state on disk for a user who thought they had switched it off.
    ("off", False),
    ("disabled", False),
    ("none", False),
    ("", False),
    ("2", False),
    ("ture", False),
]


@pytest.mark.parametrize(("value", "expected"), _ENV_CASES)
def test_env_values_gate_dumping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv(payload_dump.DUMP_ENV, value)
    assert payload_dump.enabled() is expected
    assert payload_dump.dump("x", {"a": 1}, tmp_path) is expected


def test_inherited_dump_env_is_cleared(tmp_path: Path) -> None:
    """A developer's exported PLANTRACK_* must not leak into a test run."""
    assert payload_dump.DUMP_ENV not in os.environ
    assert not payload_dump.enabled()


def test_dump_wraps_and_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(payload_dump.DUMP_ENV, "1")
    assert payload_dump.dump("zai-quota-limit", {"limits": []}, tmp_path)
    target = payload_dump.dump_dir(tmp_path) / "zai-quota-limit.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["payload"] == {"limits": []}
    assert "_fetched_at" in data
    payload_dump.dump("zai-quota-limit", {"limits": [1]}, tmp_path)
    assert json.loads(target.read_text(encoding="utf-8"))["payload"] == {"limits": [1]}


def test_fetch_remains_dumps_raw_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import REAL_FETCH_REMAINS

    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    monkeypatch.setenv(payload_dump.DUMP_ENV, "1")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "model_remains": [],
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }

    monkeypatch.setattr(minimax.requests, "get", lambda *a, **k: FakeResponse())
    REAL_FETCH_REMAINS("key", "https://www.minimaxi.com")
    files = list(payload_dump.dump_dir().glob("minimax-*-remains.json"))
    assert len(files) == 1
    assert (
        json.loads(files[0].read_text(encoding="utf-8"))["payload"]["base_resp"]["status_code"] == 0
    )


def test_fetch_limits_dumps_raw_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import REAL_FETCH_LIMITS

    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    monkeypatch.setenv(payload_dump.DUMP_ENV, "1")

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"success": True, "data": {"limits": []}}

    monkeypatch.setattr(zai.requests, "get", lambda *a, **k: FakeResponse())
    REAL_FETCH_LIMITS("key")
    target = payload_dump.dump_dir() / "zai-quota-limit.json"
    assert json.loads(target.read_text(encoding="utf-8"))["payload"]["success"] is True
