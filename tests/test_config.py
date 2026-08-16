"""Tests for the plantrack user config file."""

from __future__ import annotations

from pathlib import Path

from ai_coding_usage_tracker import config


def test_missing_config_is_empty(tmp_path: Path) -> None:
    assert config.load_config(tmp_path) == {}
    assert config.disabled_plans(tmp_path) == set()
    assert config.manual_keys(tmp_path) == {}


def test_disable_enable_roundtrip(tmp_path: Path) -> None:
    assert config.set_disabled(tmp_path, "minimax-intl", True)
    assert config.disabled_plans(tmp_path) == {"minimax-intl"}
    assert config.set_disabled(tmp_path, "minimax-intl", False)
    assert config.disabled_plans(tmp_path) == set()


def test_manual_key_roundtrip(tmp_path: Path) -> None:
    assert config.set_manual_key(tmp_path, "glm-intl", "k1", "https://api.example")
    keys = config.manual_keys(tmp_path)
    assert keys["glm-intl"]["api_key"] == "k1"
    assert keys["glm-intl"]["api_host"] == "https://api.example"
    assert config.clear_manual_key(tmp_path, "glm-intl")
    assert config.manual_keys(tmp_path) == {}


def test_manual_key_without_host_omits_field(tmp_path: Path) -> None:
    config.set_manual_key(tmp_path, "glm-intl", "k1")
    assert config.manual_keys(tmp_path)["glm-intl"] == {"api_key": "k1"}


def test_save_preserves_unrelated_keys(tmp_path: Path) -> None:
    config.set_disabled(tmp_path, "minimax-intl", True)
    raw = config.load_config(tmp_path)
    raw["future_field"] = 1
    config.save_config(tmp_path, raw)
    config.set_disabled(tmp_path, "minimax-cn", True)
    merged = config.load_config(tmp_path)
    assert merged["future_field"] == 1
    assert merged["disabled"] == ["minimax-cn", "minimax-intl"]
