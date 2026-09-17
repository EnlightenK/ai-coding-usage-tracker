"""Tests for plan discovery from local tool configurations."""

from __future__ import annotations

import json
from pathlib import Path

from ai_coding_usage_tracker import config
from ai_coding_usage_tracker.discovery import (
    MINIMAX_DEFAULT_HOSTS,
    discover_plans,
    is_zai_base_url,
    sanitize_minimax_host,
)


def _write_claude_settings(home: Path, filename: str, base_url: str | None = None) -> None:
    settings = home / ".claude" / filename
    settings.parent.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {"ANTHROPIC_AUTH_TOKEN": "key-from-settings"}
    if base_url is not None:
        env["ANTHROPIC_BASE_URL"] = base_url
    settings.write_text(json.dumps({"env": env}), encoding="utf-8")


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


def test_include_disabled_default_still_hides_disabled_plans(home: Path) -> None:
    """Regression guard: adding include_disabled must not change the default.

    Every existing caller (status, scan, plan list) relies on disabled plans
    staying hidden unless the new keyword is passed explicitly.
    """
    config.set_disabled(home, "minimax-cn", True)
    ids = {p.plan_id for p in discover_plans(home)}
    assert "minimax-cn" not in ids


def test_include_disabled_surfaces_disabled_plan_with_sources(home: Path) -> None:
    """A disabled-but-configured plan stays discoverable with the flag set.

    This is what lets `plan add --from-scan` re-add a plan the user removed:
    its key and key_sources must survive the disabled flag untouched.
    """
    config.set_disabled(home, "minimax-cn", True)
    plans = {p.plan_id: p for p in discover_plans(home, include_disabled=True)}
    assert plans["minimax-cn"].api_key == "mm-cn-key"
    assert plans["minimax-cn"].key_sources == ["~/.local/share/opencode/auth.json"]


def test_include_disabled_still_requires_key_sources(tmp_path: Path) -> None:
    """The flag widens only the disabled filter, never the key_sources one.

    A plan with no credentials on disk must stay invisible even when the
    caller asks for disabled plans, or `--from-scan` would offer to track
    plans that have nothing to track.
    """
    config.set_disabled(tmp_path, "claude-code", True)
    assert discover_plans(tmp_path, include_disabled=True) == []


def test_forgotten_plan_excluded_by_default(home: Path) -> None:
    config.set_forgotten(home, "minimax-cn", True)
    ids = {p.plan_id for p in discover_plans(home)}
    assert "minimax-cn" not in ids


def test_forgotten_plan_excluded_even_with_include_disabled(home: Path) -> None:
    """Forgotten means "never show", so it survives the include_disabled widening.

    The flag exists to re-surface *disabled* plans for `plan add --from-scan`;
    a forgotten plan was deliberately removed and must stay hidden even then —
    forgotten is a stronger exclusion than disabled.
    """
    config.set_forgotten(home, "minimax-cn", True)
    ids = {p.plan_id for p in discover_plans(home, include_disabled=True)}
    assert "minimax-cn" not in ids


def test_forgotten_plan_returns_after_marker_cleared(home: Path) -> None:
    config.set_forgotten(home, "minimax-cn", True)
    assert "minimax-cn" not in {p.plan_id for p in discover_plans(home)}
    config.set_forgotten(home, "minimax-cn", False)
    plans = {p.plan_id: p for p in discover_plans(home)}
    assert plans["minimax-cn"].api_key == "mm-cn-key"


FALLBACK = "https://fallback.example"


def test_sanitize_maps_alias_to_canonical_host() -> None:
    assert sanitize_minimax_host("https://api.minimaxi.com", FALLBACK) == "https://www.minimaxi.com"
    assert sanitize_minimax_host("https://api.minimax.io", FALLBACK) == "https://www.minimax.io"


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


def test_glm_settings_without_a_base_url_still_matches_on_filename(
    tmp_path: Path,
) -> None:
    """The common case: `settings-glm.json` naming the token and nothing else."""
    _write_claude_settings(tmp_path, "settings-glm.json")
    plans = {p.plan_id: p for p in discover_plans(tmp_path)}
    assert plans["glm-intl"].api_key == "key-from-settings"
    assert "~/.claude/settings-glm.json" in plans["glm-intl"].key_sources


def test_glm_settings_accepts_z_ai_base_urls(tmp_path: Path) -> None:
    for index, base_url in enumerate(
        (
            "https://api.z.ai/api/anthropic",
            "https://open.z.ai/api/anthropic",
            "https://z.ai/api/anthropic",
        )
    ):
        fake_home = tmp_path / f"home{index}"
        _write_claude_settings(fake_home, "settings-glm.json", base_url)
        plans = {p.plan_id: p for p in discover_plans(fake_home)}
        assert plans["glm-intl"].api_key == "key-from-settings", base_url


def test_glm_settings_for_another_provider_is_skipped(tmp_path: Path) -> None:
    """A `-glm` filename alone must not decide where a token is sent.

    GLM's quota endpoint is a hardcoded Z.ai URL, so reusing the filename for
    another Anthropic-compatible provider used to hand that provider's key to
    Z.ai. A declared non-Z.ai base URL now disqualifies the file instead.
    """
    _write_claude_settings(tmp_path, "settings-glm.json", "https://api.some-other-provider.example")
    plans = {p.plan_id: p for p in discover_plans(tmp_path)}
    assert "glm-intl" not in plans


def test_glm_settings_key_is_skipped_for_a_lookalike_or_plaintext_host(
    tmp_path: Path,
) -> None:
    for index, base_url in enumerate(
        (
            "http://api.z.ai/api/anthropic",
            "https://z.ai.evil.example/api/anthropic",
            "https://evil-z.ai/api/anthropic",
            "https://z.ai@evil.example/api/anthropic",
            "https://open.bigmodel.cn/api/anthropic",
            "not a url",
        )
    ):
        fake_home = tmp_path / f"home{index}"
        _write_claude_settings(fake_home, "settings-glm.json", base_url)
        plans = {p.plan_id: p for p in discover_plans(fake_home)}
        assert "glm-intl" not in plans, base_url


def test_glm_settings_key_still_loses_to_an_earlier_source(home: Path) -> None:
    """A hostile settings file cannot displace the opencode key either."""
    _write_claude_settings(home, "settings-glm.json", "https://evil.example")
    plans = {p.plan_id: p for p in discover_plans(home)}
    assert plans["glm-intl"].api_key == "zai-key"


def test_is_zai_base_url() -> None:
    assert is_zai_base_url("https://api.z.ai/api/anthropic")
    assert is_zai_base_url("https://z.ai")
    assert is_zai_base_url("https://api.z.ai:8443/api/anthropic")
    assert not is_zai_base_url("http://api.z.ai")
    assert not is_zai_base_url("https://api.z.ai.evil.example")
    assert not is_zai_base_url("https://evilz.ai")
    assert not is_zai_base_url("https://open.bigmodel.cn/api/anthropic")
    assert not is_zai_base_url("")
    assert not is_zai_base_url("not a url")
