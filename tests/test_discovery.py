"""Tests for plan discovery from local tool configurations."""

from __future__ import annotations

import json
from pathlib import Path

from ai_coding_usage_tracker import config
from ai_coding_usage_tracker.discovery import (
    MINIMAX_DEFAULT_HOSTS,
    discover_plans,
    sanitize_minimax_host,
)


def _write_claude_settings(home: Path, filename: str, base_url: str) -> None:
    settings = home / ".claude" / filename
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "key-from-settings",
                    "ANTHROPIC_BASE_URL": base_url,
                }
            }
        ),
        encoding="utf-8",
    )


def test_discovers_all_five_plans(home: Path) -> None:
    plans = {p.plan_id: p for p in discover_plans(home)}
    assert set(plans) == {
        "minimax-cn",
        "minimax-intl",
        "glm-intl",
        "claude-code",
        "chatgpt-codex",
    }


def test_minimax_intl_key_from_codex_config(home: Path) -> None:
    plans = {p.plan_id: p for p in discover_plans(home)}
    intl = plans["minimax-intl"]
    assert intl.api_key == "mm-intl-key"
    assert intl.api_host == "https://www.minimax.io"


def test_minimax_cn_key_prefers_opencode_auth(home: Path) -> None:
    plans = {p.plan_id: p for p in discover_plans(home)}
    cn = plans["minimax-cn"]
    assert cn.api_key == "mm-cn-key"
    assert cn.api_host == "https://www.minimaxi.com"


def test_glm_key_from_opencode_auth(home: Path) -> None:
    plans = {p.plan_id: p for p in discover_plans(home)}
    glm = plans["glm-intl"]
    assert glm.api_key == "zai-key"


def test_oauth_plans_report_sources(home: Path) -> None:
    plans = {p.plan_id: p for p in discover_plans(home)}
    assert plans["claude-code"].key_sources
    assert plans["chatgpt-codex"].key_sources


def test_empty_home_discovers_nothing(tmp_path: Path) -> None:
    assert discover_plans(tmp_path) == []


def test_glm_key_from_claude_settings_file(tmp_path: Path) -> None:
    """Regression: a GLM settings file must not crash the MiniMax host lookup."""
    _write_claude_settings(tmp_path, "settings-glm.json", "https://api.z.ai/api/anthropic")
    plans = {p.plan_id: p for p in discover_plans(tmp_path)}
    assert plans["glm-intl"].api_key == "key-from-settings"
    # The GLM quota API has a fixed URL; no host is derived from the base URL.
    assert plans["glm-intl"].api_host is None


def test_minimax_cn_key_from_claude_settings_file(tmp_path: Path) -> None:
    _write_claude_settings(tmp_path, "settings-mx-cn.json", "https://api.minimaxi.com")
    plans = {p.plan_id: p for p in discover_plans(tmp_path)}
    assert plans["minimax-cn"].api_key == "key-from-settings"
    assert plans["minimax-cn"].api_host == "https://www.minimaxi.com"


def test_manual_key_from_config_tracks_plan(tmp_path: Path) -> None:
    """A key stored by `plantrack plan add` tracks a plan nothing else configures."""
    config.set_manual_key(tmp_path, "glm-intl", "manual-key")
    plans = {p.plan_id: p for p in discover_plans(tmp_path)}
    assert plans["glm-intl"].api_key == "manual-key"
    assert "plantrack config" in plans["glm-intl"].key_sources


def test_disabled_plan_excluded(home: Path) -> None:
    config.set_disabled(home, "minimax-intl", True)
    ids = {p.plan_id for p in discover_plans(home)}
    assert "minimax-intl" not in ids
    assert "minimax-cn" in ids


FALLBACK = "https://fallback.example"


def test_sanitize_maps_alias_to_canonical_host() -> None:
    assert (
        sanitize_minimax_host("https://api.minimaxi.com", FALLBACK)
        == "https://www.minimaxi.com"
    )
    assert (
        sanitize_minimax_host("https://api.minimax.io", FALLBACK)
        == "https://www.minimax.io"
    )


def test_sanitize_accepts_https_minimax_domain() -> None:
    assert sanitize_minimax_host("https://www.minimaxi.com", FALLBACK) == (
        "https://www.minimaxi.com"
    )
    assert sanitize_minimax_host("https://quota.minimax.io:8443", FALLBACK) == (
        "https://quota.minimax.io:8443"
    )


def test_sanitize_rejects_http_and_returns_default() -> None:
    assert sanitize_minimax_host("http://www.minimaxi.com", FALLBACK) == FALLBACK


def test_sanitize_rejects_foreign_and_lookalike_domains() -> None:
    assert sanitize_minimax_host("https://evil.example", FALLBACK) == FALLBACK
    assert sanitize_minimax_host("https://minimaxi.com.evil.example", FALLBACK) == FALLBACK
    assert sanitize_minimax_host("https://evil-minimaxi.com", FALLBACK) == FALLBACK
    assert sanitize_minimax_host("https://minimaxi.com@evil.example", FALLBACK) == FALLBACK


def test_sanitize_rejects_garbage() -> None:
    assert sanitize_minimax_host("not a url", FALLBACK) == FALLBACK
    assert sanitize_minimax_host("", FALLBACK) == FALLBACK


def test_manual_key_with_hostile_api_host_falls_back_to_default(tmp_path: Path) -> None:
    """A hostile api_host in the config must never receive the stored key."""
    config.set_manual_key(tmp_path, "minimax-intl", "manual-key", "http://evil.example")
    plans = {p.plan_id: p for p in discover_plans(tmp_path)}
    assert plans["minimax-intl"].api_host == MINIMAX_DEFAULT_HOSTS["minimax-intl"]


def test_manual_key_ignores_api_host_for_glm(tmp_path: Path) -> None:
    """GLM has a fixed quota endpoint; a config api_host must be ignored."""
    config.set_manual_key(tmp_path, "glm-intl", "manual-key", "http://evil.example")
    plans = {p.plan_id: p for p in discover_plans(tmp_path)}
    assert plans["glm-intl"].api_host is None


def test_claude_settings_hostile_base_url_falls_back_to_default(tmp_path: Path) -> None:
    _write_claude_settings(tmp_path, "settings-mx-cn.json", "https://evil.example")
    plans = {p.plan_id: p for p in discover_plans(tmp_path)}
    assert plans["minimax-cn"].api_host == MINIMAX_DEFAULT_HOSTS["minimax-cn"]
