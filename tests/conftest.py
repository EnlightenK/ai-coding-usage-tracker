"""Shared fixtures: a fake home directory tree with representative tool logs."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

from ai_coding_usage_tracker import cli
from ai_coding_usage_tracker.providers import claude_profile, codex, minimax, zai
from ai_coding_usage_tracker.providers.codex_app_server import CodexAppServerUnavailable


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_lines(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def fake_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


@pytest.fixture(autouse=True)
def no_real_codex_app_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fixture homes hermetic; focused app-server tests opt in explicitly."""
    def unavailable(*_: object, **__: object) -> object:
        raise CodexAppServerUnavailable("mocked Codex app-server unavailable")

    monkeypatch.setattr(codex, "fetch_quotas", unavailable)
    monkeypatch.setattr(codex, "fetch_remote_usage", unavailable)


@pytest.fixture(autouse=True)
def plain_console_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep rich output uncoloured so CLI assertions match plain text.

    Some terminals (and agent harnesses) export FORCE_COLOR, which makes the
    module-level consoles emit ANSI escapes into the captured CLI output.
    """
    monkeypatch.setattr(cli, "console", Console(color_system=None))
    monkeypatch.setattr(cli, "err_console", Console(stderr=True, color_system=None))


# Captured before the autouse guards below replace them, so the fetch itself can
# still be tested (against a fake transport) without fighting the guard.
REAL_FETCH_PROFILE = claude_profile.fetch_profile
REAL_FETCH_REMAINS = minimax.fetch_remains
REAL_FETCH_LIMITS = zai.fetch_limits


@pytest.fixture(autouse=True)
def no_real_quota_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never call the MiniMax or Z.ai quota APIs from tests."""

    def fake_remains(api_key: str, host: str, timeout: float = 15.0) -> minimax.MiniMaxRemains:
        return minimax.MiniMaxRemains(active=None, note="quota fetch disabled in tests")

    def fake_limits(api_key: str, timeout: float = 15.0) -> zai.ZaiQuota:
        return zai.ZaiQuota(active=None, note="quota fetch disabled in tests")

    monkeypatch.setattr(minimax, "fetch_remains", fake_remains)
    monkeypatch.setattr(zai, "fetch_limits", fake_limits)


@pytest.fixture(autouse=True)
def no_real_claude_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never call the Anthropic profile API from tests.

    Tests that need profile data write a cache file into the fixture home,
    which exercises the parsing path without any patching.
    """
    def offline(*_: object, **__: object) -> tuple[None, str]:
        return None, "profile fetch disabled in tests"

    monkeypatch.setattr(claude_profile, "fetch_profile", offline)


def write_profile_cache(
    home: Path, profile: dict, fetched_at: datetime | None = None
) -> None:
    """Seed the Claude account profile cache inside a fixture home."""
    stamp = fetched_at or datetime.now(tz=timezone.utc)
    write_json(
        home / ".claude" / claude_profile.CACHE_FILENAME,
        {"fetched_at": stamp.isoformat(), "profile": profile},
    )


def claude_profile_payload(**organization: object) -> dict:
    """Build a profile payload shaped like the real oauth profile response."""
    org = {
        "organization_type": "claude_pro",
        "billing_type": "apple_subscription",
        "rate_limit_tier": "default_claude_ai",
        "seat_tier": None,
        "subscription_status": "incomplete",
        "subscription_created_at": "2026-02-27T14:04:33.624119Z",
        "claude_code_trial_ends_at": None,
    }
    org.update(organization)
    return {
        "account": {
            "email": "user@example.com",
            "has_claude_pro": True,
            "has_claude_max": False,
        },
        "organization": org,
        "application": {"slug": "claude-code"},
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    now_ms = 1786900000000

    write_json(
        tmp_path / ".local" / "share" / "opencode" / "auth.json",
        {
            "zai-coding-plan": {"type": "api", "key": "zai-key"},
            "minimax-cn-coding-plan": {"type": "api", "key": "mm-cn-key"},
        },
    )

    write_json(
        tmp_path / ".claude" / ".credentials.json",
        {
            "claudeAiOauth": {
                "accessToken": "x",
                "refreshToken": "y",
                "expiresAt": now_ms + 3_600_000,
                "refreshTokenExpiresAt": now_ms + 25 * 86400_000,
                "subscriptionType": "pro",
            }
        },
    )

    write_json(
        tmp_path / ".codex" / "auth.json",
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": fake_jwt(
                    {
                        "email": "user@example.com",
                        "https://api.openai.com/auth": {
                            "chatgpt_plan_type": "plus",
                            "chatgpt_subscription_active_until": "2026-09-10T17:13:25+00:00",
                        },
                    }
                )
            },
        },
    )

    (tmp_path / ".codex" / "config.toml").write_text(
        'model = "gpt-5.6-sol"\n'
        "[mcp_servers.MiniMax]\n"
        'command = "uvx"\n'
        "args = [\"minimax-coding-plan-mcp\"]\n"
        "\n"
        "[mcp_servers.MiniMax.env]\n"
        'MINIMAX_API_KEY = "mm-intl-key"\n'
        'MINIMAX_API_HOST = "https://api.minimax.io"\n',
        encoding="utf-8",
    )

    transcript = [
        {"type": "mode", "mode": "normal"},
        {
            "type": "assistant",
            "timestamp": "2026-08-15T10:00:00.000Z",
            "message": {
                "id": "msg_1",
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 200,
                },
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-15T11:00:00.000Z",
            "message": {
                "id": "msg_1",
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 999,
                    "output_tokens": 999,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-15T12:00:00.000Z",
            "message": {
                "id": "msg_2",
                "model": "glm-5.2",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 2,
                },
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-15T13:00:00.000Z",
            "message": {
                "id": "msg_3",
                "model": "MiniMax-M3",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    ]
    project_dir = tmp_path / ".claude" / "projects" / "demo"
    write_lines(project_dir / "session.jsonl", transcript)

    codex_session = [
        {
            "timestamp": "2026-08-15T19:31:11.286Z",
            "type": "session_meta",
            "payload": {
                "session_id": "s1",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-08-15T19:31:12.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cached_input_tokens": 100,
                    },
                    "last_token_usage": {"input_tokens": 10},
                },
            },
        },
        {
            "timestamp": "2026-08-15T19:35:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 30,
                        "output_tokens": 15,
                        "cached_input_tokens": 300,
                        "reasoning_output_tokens": 8,
                    }
                },
            },
        },
    ]
    write_lines(
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "08"
        / "15"
        / "rollout-demo.jsonl",
        codex_session,
    )

    write_json(
        tmp_path / ".local" / "share" / "opencode" / "storage" / "message" / "ses_1"
        / "msg_zai.json",
        {
            "id": "msg_zai",
            "role": "assistant",
            "time": {"created": 1786800000000},
            "model": {"providerID": "zai-coding-plan", "modelID": "glm-5.2"},
        },
    )
    write_json(
        tmp_path / ".local" / "share" / "opencode" / "storage" / "message" / "ses_1"
        / "msg_other.json",
        {
            "id": "msg_other",
            "role": "assistant",
            "time": {"created": 1786800000000},
            "model": {"providerID": "nvidia", "modelID": "llama"},
        },
    )
    part_dir = (
        tmp_path / ".local" / "share" / "opencode" / "storage" / "part" / "msg_zai"
    )
    write_json(
        part_dir / "part_1.json",
        {
            "id": "part_1",
            "messageID": "msg_zai",
            "type": "step-finish",
            "tokens": {
                "input": 500,
                "output": 80,
                "reasoning": 10,
                "cache": {"read": 40, "write": 4},
            },
        },
    )
    write_json(
        part_dir / "part_2.json",
        {
            "id": "part_2",
            "messageID": "msg_other",
            "type": "step-finish",
            "tokens": {"input": 777, "output": 1},
        },
    )

    return tmp_path
