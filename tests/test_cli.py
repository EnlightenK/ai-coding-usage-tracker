"""CLI integration tests using Typer's CliRunner and a fake PLANTRACK_HOME."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import claude_profile_payload, write_json, write_profile_cache
from typer.testing import CliRunner

from ai_coding_usage_tracker import __version__, config
from ai_coding_usage_tracker.cli import _fmt_subscription, app
from ai_coding_usage_tracker.models import QuotaWindow
from ai_coding_usage_tracker.providers import codex
from ai_coding_usage_tracker.providers.minimax import MiniMaxRemains
from ai_coding_usage_tracker.tracker import collect_statuses

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_refresh_claude_error_names_current_key_location(
    fake_env: pytest.MonkeyPatch,
) -> None:
    """The no-key instructions must point at ~/.local/ptk/session-key, not the
    removed repo-root location (review finding: upgrading users followed the
    stale text into a failure loop)."""
    result = runner.invoke(app, ["refresh-claude"])
    assert result.exit_code == 2
    assert "~/.local/ptk/session-key" in result.stderr
    assert "project's" not in result.stderr
    assert "PLANTRACK_SESSION_KEY_FILE" in result.stderr


def test_refresh_claude_reports_statusline_windows_without_a_key(
    home: Path, fake_env: pytest.MonkeyPatch
) -> None:
    """A missing session key is not a failure while the statusline capture is
    fresh: those windows need no credential, so report them and exit 0 instead
    of sending the user to the browser for a cookie they do not need."""
    write_json(
        home / ".local" / "ptk" / "claude-rate-limits.json",
        {
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
            "five_hour": {"used_percentage": 21.0},
            "seven_day": {"used_percentage": 34.0},
        },
    )
    result = runner.invoke(app, ["refresh-claude"])
    assert result.exit_code == 0
    assert "statusline capture" in result.output
    assert "21% used" in result.output
    assert "34% used" in result.output
    assert "optional" in result.output


@pytest.fixture
def fake_env(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> pytest.MonkeyPatch:
    monkeypatch.setenv("PLANTRACK_HOME", str(home))
    monkeypatch.setenv("COLUMNS", "300")

    def fake_fetch(api_key: str, host: str, timeout: float = 15.0) -> MiniMaxRemains:
        if "intl" in api_key or "minimax.io" in host:
            return MiniMaxRemains(
                active=False, note="API error 2062: no active token plan subscription"
            )
        return MiniMaxRemains(
            active=True,
            quotas=[
                QuotaWindow(kind="5h", remaining_percent=92.0, resets_at=None),
                QuotaWindow(kind="weekly", remaining_percent=78.0, resets_at=None),
            ],
        )

    monkeypatch.setattr(
        "ai_coding_usage_tracker.providers.minimax.fetch_remains", fake_fetch
    )

    def fake_zai(api_key: str, timeout: float = 15.0):
        from ai_coding_usage_tracker.models import QuotaWindow
        from ai_coding_usage_tracker.providers.zai import ZaiQuota

        return ZaiQuota(
            active=True,
            quotas=[
                QuotaWindow(kind="5h", remaining_percent=51.0, resets_at=None),
                QuotaWindow(kind="weekly", remaining_percent=73.0, resets_at=None),
            ],
        )

    monkeypatch.setattr(
        "ai_coding_usage_tracker.providers.zai.fetch_limits", fake_zai
    )
    return monkeypatch


def test_status_table(fake_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "MiniMax Coding Plan (CN)" in result.output
    assert "GLM Coding Plan" in result.output
    assert "Claude Code" in result.output
    assert "ChatGPT Codex" in result.output


def test_status_json(fake_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 5
    by_id = {p["plan_id"]: p for p in payload}
    assert by_id["minimax-cn"]["active"] is True
    assert by_id["minimax-intl"]["active"] is False
    assert by_id["claude-code"]["subscription"]["plan_type"] == "pro"
    assert by_id["chatgpt-codex"]["subscription"]["plan_type"] == "plus"


def test_status_warns_on_unknown_tz(fake_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["status", "--tz", "Mars/Olympus_Mons"])
    assert result.exit_code == 0
    assert "Unknown timezone 'Mars/Olympus_Mons'" in result.stderr
    assert "AI Coding Plans" in result.stdout


def test_status_accepts_env_tz(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    fake_env.setenv("PLANTRACK_TZ", "Asia/Tokyo")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "UTC+09:00" in result.stdout


def _claude_subscription_cell(home: Path) -> str:
    """Render the Subscription cell for the Claude plan, unwrapped by a table."""
    claude_status = {s.plan_id: s for s in collect_statuses(home)}["claude-code"]
    return _fmt_subscription(claude_status)


def test_status_json_carries_subscription_detail(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    write_profile_cache(home, claude_profile_payload())
    payload = json.loads(runner.invoke(app, ["status", "--json"]).output)
    claude_plan = {p["plan_id"]: p for p in payload}["claude-code"]["subscription"]
    assert claude_plan["plan_type"] == "pro"
    assert claude_plan["status"] == "incomplete"
    assert claude_plan["billing"] == "apple_subscription"
    assert claude_plan["days_left"] is None
    assert claude_plan["auth_days_left"] is not None


def test_subscription_cell_has_no_countdown_for_an_open_ended_plan(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    """An App Store Pro plan is just 'pro': no fake countdown, no alarm word."""
    write_profile_cache(home, claude_profile_payload())
    cell = _claude_subscription_cell(home)
    assert "pro" in cell
    assert "incomplete" not in cell
    assert "left" not in cell
    # The credential lifetime is far off, so it stays out of the column too.
    assert "auth" not in cell


def test_subscription_cell_flags_a_real_billing_problem(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    write_profile_cache(
        home,
        claude_profile_payload(billing_type="stripe", subscription_status="past_due"),
    )
    assert "past_due" in _claude_subscription_cell(home)
    assert "past_due" in runner.invoke(app, ["status"]).output


def test_subscription_cell_counts_down_a_trial(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    ends = datetime.now(tz=timezone.utc) + timedelta(days=5)
    write_profile_cache(
        home, claude_profile_payload(claude_code_trial_ends_at=ends.isoformat())
    )
    assert "5d left" in _claude_subscription_cell(home)
    assert "5d left" in runner.invoke(app, ["status"]).output


def test_subscription_cell_warns_when_reauth_is_due(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    """A soon-to-expire OAuth token is shown as auth state, not as billing."""
    credentials = json.loads(
        (home / ".claude" / ".credentials.json").read_text(encoding="utf-8")
    )
    soon = datetime.now(tz=timezone.utc) + timedelta(days=3)
    credentials["claudeAiOauth"]["refreshTokenExpiresAt"] = int(
        soon.timestamp() * 1000
    )
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps(credentials), encoding="utf-8"
    )
    write_profile_cache(home, claude_profile_payload())
    cell = _claude_subscription_cell(home)
    assert "(auth 3d)" in cell
    assert "left" not in cell


def test_usage_table(fake_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["usage", "--days", "3650"])
    assert result.exit_code == 0
    assert "glm-5.2" in result.output
    assert "codex" in result.output
    assert "TOTAL" in result.output


def test_usage_plan_filter(fake_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["usage", "--days", "3650", "--plan", "glm-intl"])
    assert result.exit_code == 0
    assert "glm-5.2" in result.output
    assert "Claude Code" not in result.output


def test_usage_unknown_plan_errors(fake_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["usage", "--plan", "nope"])
    assert result.exit_code == 2


def test_usage_json(fake_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["usage", "--days", "3650", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert any(r["plan_id"] == "glm-intl" for r in payload)


def test_capture_claude_command(fake_env: pytest.MonkeyPatch) -> None:
    import time

    payload = json.dumps(
        {
            "session_id": "s1",
            "rate_limits": {
                "five_hour": {"used_percentage": 10.0, "resets_at": int(time.time()) + 3600},
                "seven_day": {"used_percentage": 50.0, "resets_at": int(time.time()) + 7200},
            },
        }
    )
    result = runner.invoke(app, ["capture-claude"], input=payload)
    assert result.exit_code == 0
    statuses = {s.plan_id: s for s in collect_statuses()}
    quotas = {q.kind: q for q in statuses["claude-code"].quotas}
    assert quotas["5h"].remaining_percent == 90.0
    assert quotas["weekly"].remaining_percent == 50.0


def test_capture_claude_rejects_bad_json(fake_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["capture-claude"], input="not json")
    assert result.exit_code == 1


def test_codex_login_command_prints_device_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_login(show_code, *, home: Path, timeout: float) -> None:
        assert timeout == 12.0
        show_code(codex.DeviceLogin("https://auth.openai.com/codex/device", "ABCD-1234", "login"))

    monkeypatch.setattr(codex, "login_with_device_code", fake_login)
    result = runner.invoke(app, ["codex-login", "--timeout", "12"])
    assert result.exit_code == 0
    assert "https://auth.openai.com/codex/device" in result.output
    assert "ABCD-1234" in result.output
    assert "authenticated" in result.output


def _status_plan_ids() -> set[str]:
    payload = json.loads(runner.invoke(app, ["status", "--json"]).output)
    return {p["plan_id"] for p in payload}


def test_plan_disable_hides_plan_from_status(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    result = runner.invoke(app, ["plan", "disable", "minimax-intl"])
    assert result.exit_code == 0
    assert "minimax-intl" not in _status_plan_ids()


def test_plan_enable_restores_plan(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    runner.invoke(app, ["plan", "disable", "minimax-intl"])
    result = runner.invoke(app, ["plan", "enable", "minimax-intl"])
    assert result.exit_code == 0
    assert "minimax-intl" in _status_plan_ids()


def test_plan_remove_disables_discovered_plan(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    result = runner.invoke(app, ["plan", "remove", "minimax-intl"])
    assert result.exit_code == 0
    assert "minimax-intl" not in _status_plan_ids()


def test_plan_add_tracks_plan_on_unconfigured_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    assert _status_plan_ids() == set()
    result = runner.invoke(app, ["plan", "add", "glm-intl", "--api-key", "manual-key"])
    assert result.exit_code == 0
    assert _status_plan_ids() == {"glm-intl"}


def test_plan_add_rejects_unknown_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    assert runner.invoke(app, ["plan", "add", "nope", "--api-key", "k"]).exit_code == 2


def test_plan_add_rejects_untrusted_api_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    result = runner.invoke(
        app,
        ["plan", "add", "minimax-cn", "--api-key", "k", "--api-host", "http://evil.example"],
    )
    assert result.exit_code == 2
    assert "api-host" in result.stderr.lower()
    assert config.manual_keys(tmp_path) == {}


def test_plan_add_rejects_api_host_for_non_minimax_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    result = runner.invoke(
        app,
        ["plan", "add", "glm-intl", "--api-key", "k", "--api-host", "https://www.minimaxi.com"],
    )
    assert result.exit_code == 2
    assert config.manual_keys(tmp_path) == {}


def test_plan_add_accepts_and_normalizes_minimax_api_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    result = runner.invoke(
        app,
        ["plan", "add", "minimax-cn", "--api-key", "k", "--api-host", "https://api.minimaxi.com"],
    )
    assert result.exit_code == 0
    entry = config.manual_keys(tmp_path)["minimax-cn"]
    assert entry["api_host"] == "https://www.minimaxi.com"


def test_plan_add_prompts_for_key_when_flag_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    result = runner.invoke(app, ["plan", "add", "glm-intl"], input="secret\n")
    assert result.exit_code == 0
    assert config.manual_keys(tmp_path)["glm-intl"]["api_key"] == "secret"
    assert _status_plan_ids() == {"glm-intl"}


def test_plan_disable_rejects_unknown_plan(tmp_path: Path) -> None:
    assert runner.invoke(app, ["plan", "disable", "nope"]).exit_code == 2


def test_plan_list_shows_states(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    runner.invoke(app, ["plan", "disable", "minimax-intl"])
    result = runner.invoke(app, ["plan", "list"])
    assert result.exit_code == 0
    assert "MiniMax Coding Plan (Intl)" in result.output
    assert "disabled" in result.output
    assert "tracked" in result.output


def test_scan_reports_files_logs_and_plans(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0
    assert "Codex credentials" in result.output
    assert "Claude Code transcripts" in result.output
    assert "Discovered plans" in result.output


def test_scan_json(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    payload = json.loads(runner.invoke(app, ["scan", "--json"]).output)
    assert set(payload) == {"files", "logs", "plans"}
    assert any(entry["found"] for entry in payload["files"])
    assert {p["plan_id"] for p in payload["plans"]} == {
        "minimax-cn",
        "minimax-intl",
        "glm-intl",
        "claude-code",
        "chatgpt-codex",
    }


def test_status_refresh_flag(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    result = runner.invoke(app, ["status", "--refresh", "--json"])
    assert result.exit_code == 0
    assert len(json.loads(result.output)) == 5


def test_usage_run_records_history(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    runner.invoke(app, ["usage", "--days", "3650"])
    payload = json.loads(
        runner.invoke(app, ["history", "usage", "--days", "3650", "--json"]).output
    )
    assert {r["plan_id"] for r in payload} == {
        "claude-code",
        "glm-intl",
        "minimax-cn",
        "chatgpt-codex",
    }


def test_history_usage_table(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    runner.invoke(app, ["usage", "--days", "3650"])
    result = runner.invoke(app, ["history", "usage", "--days", "3650"])
    assert result.exit_code == 0
    assert "TOTAL" in result.output


def test_history_usage_plan_filter(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    runner.invoke(app, ["usage", "--days", "3650"])
    payload = json.loads(
        runner.invoke(app, ["history", "usage", "--plan", "glm-intl", "--json"]).output
    )
    assert {r["plan_id"] for r in payload} == {"glm-intl"}


def test_history_usage_empty_store(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    result = runner.invoke(app, ["history", "usage"])
    assert result.exit_code == 0
    assert "No recorded usage" in result.output


def test_history_status_after_status_run(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    runner.invoke(app, ["status", "--json"])
    result = runner.invoke(app, ["history", "status", "--hours", "1"])
    assert result.exit_code == 0
    assert "Claude Code" in result.output
    payload = json.loads(
        runner.invoke(app, ["history", "status", "--hours", "1", "--json"]).output
    )
    assert {row["plan_id"] for row in payload} == {
        "minimax-cn",
        "minimax-intl",
        "glm-intl",
        "claude-code",
        "chatgpt-codex",
    }


def test_history_status_empty_store(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    result = runner.invoke(app, ["history", "status"])
    assert result.exit_code == 0
    assert "No status snapshots" in result.output


def test_history_rejects_unknown_plan(fake_env: pytest.MonkeyPatch, home: Path) -> None:
    assert runner.invoke(app, ["history", "usage", "--plan", "nope"]).exit_code == 2
    assert runner.invoke(app, ["history", "status", "--plan", "nope"]).exit_code == 2


def test_status_ages_the_capture_at_display_time(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    """A note served from the status cache must not keep quoting a stale age."""
    from rich.console import Console

    from ai_coding_usage_tracker import cli
    from ai_coding_usage_tracker.providers import claude_limits

    # Wide enough that the Note cell is not folded across lines.
    fake_env.setattr(cli, "console", Console(color_system=None, width=400))
    claude_limits.capture_from_statusline_json(
        {"rate_limits": {"five_hour": {"used_percentage": 23.5}}}, home
    )
    first = runner.invoke(app, ["status"])
    assert first.exit_code == 0
    assert "rate limits as of 0s ago" in first.output

    # Backdate the capture (still inside the 6h freshness limit) without
    # touching the status cache, exactly as a quiet statusline does.
    target = claude_limits.cache_file(home)
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    snapshot["captured_at"] = (
        datetime.now(tz=timezone.utc) - timedelta(hours=2)
    ).isoformat()
    target.write_text(json.dumps(snapshot), encoding="utf-8")

    second = runner.invoke(app, ["status"])
    assert second.exit_code == 0
    assert "rate limits as of 2h0m ago" in second.output
    assert "rate limits as of 0s ago" not in second.output


# An MCP server name is copied verbatim into the key-source string, so a
# hostile ~/.codex/config.toml decides what the "Key sources" column contains.
HOSTILE_SERVER = "[bold red]SPOOFED - verified by Anthropic[/bold red]"
HOSTILE_SOURCE = f"~/.codex/config.toml [{HOSTILE_SERVER}]"


def _write_hostile_codex_config(home: Path) -> None:
    """Point the MiniMax Intl key at an MCP server whose name is rich markup."""
    (home / ".codex" / "config.toml").write_text(
        f'[mcp_servers."{HOSTILE_SERVER}"]\n'
        'command = "uvx"\n'
        "\n"
        f'[mcp_servers."{HOSTILE_SERVER}".env]\n'
        'MINIMAX_API_KEY = "mm-intl-key"\n',
        encoding="utf-8",
    )


@pytest.fixture
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep table cells on one line so literal text survives as a substring."""
    from rich.console import Console

    from ai_coding_usage_tracker import cli

    monkeypatch.setattr(cli, "console", Console(color_system=None, width=400))


def test_plan_list_escapes_markup_in_key_sources(
    fake_env: pytest.MonkeyPatch, home: Path, wide_console: None
) -> None:
    """A hostile MCP server name must not crash `plan list` (unbalanced tags
    raised MarkupError) nor style the column a user reads to check a key's
    provenance."""
    _write_hostile_codex_config(home)
    result = runner.invoke(app, ["plan", "list"])
    assert result.exit_code == 0
    assert HOSTILE_SOURCE in result.output


def test_scan_escapes_markup_in_key_sources(
    fake_env: pytest.MonkeyPatch, home: Path, wide_console: None
) -> None:
    _write_hostile_codex_config(home)
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0
    assert HOSTILE_SOURCE in result.output


def test_plan_list_shows_each_key_source_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_console: None
) -> None:
    """Discovery already reports a stored key as `plantrack config`; the manual
    prepend must not print it a second time."""
    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    assert runner.invoke(app, ["plan", "add", "minimax-cn", "--api-key", "k"]).exit_code == 0
    result = runner.invoke(app, ["plan", "list"])
    assert result.exit_code == 0
    assert "tracked" in result.output
    assert result.output.count("plantrack config") == 1


def test_plan_list_keeps_the_source_of_a_disabled_plans_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_console: None
) -> None:
    """`discover_plans` skips disabled plans, so the manual prepend is the only
    thing that still says where a disabled plan's key lives."""
    monkeypatch.setenv("PLANTRACK_HOME", str(tmp_path))
    runner.invoke(app, ["plan", "add", "minimax-cn", "--api-key", "k"])
    runner.invoke(app, ["plan", "disable", "minimax-cn"])
    result = runner.invoke(app, ["plan", "list"])
    assert result.exit_code == 0
    assert "disabled" in result.output
    assert result.output.count("plantrack config") == 1


def test_status_escapes_markup_in_provider_notes(
    fake_env: pytest.MonkeyPatch, home: Path
) -> None:
    """A provider-controlled note must print verbatim, not as rich markup."""

    def evil_fetch(api_key: str, host: str, timeout: float = 15.0) -> MiniMaxRemains:
        return MiniMaxRemains(active=None, note="quota [red]FAKE[/red] injected")

    fake_env.setattr(
        "ai_coding_usage_tracker.providers.minimax.fetch_remains", evil_fetch
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "FAKE" in result.output
    # Escaped brackets reach the terminal as literal text; unescaped markup
    # would instead be interpreted by rich and the brackets swallowed.
    assert "[red]FAKE[/red]" in result.output
