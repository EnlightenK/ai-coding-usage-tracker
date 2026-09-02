"""Focused tests for Codex app-server transport and remote usage mapping."""

from __future__ import annotations

import io
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from ai_coding_usage_tracker.models import QuotaWindow
from ai_coding_usage_tracker.providers import codex
from ai_coding_usage_tracker.providers.codex import (
    fetch_remote_usage as fetch_remote_usage_unmocked,
)
from ai_coding_usage_tracker.providers.codex_app_server import CodexAppServer, _child_env
from ai_coding_usage_tracker.tracker import collect_statuses


def fake_server_factory(process: FakeProcess):
    """Build the monkeypatch replacement for CodexAppServer around one process."""
    return lambda **kwargs: CodexAppServer(
        process_factory=lambda *_args, **_kwargs: process, **kwargs
    )


class FakeProcess:
    def __init__(self, messages: list[dict]) -> None:
        self.stdin = _InspectableStdin()
        self.stdout = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
        self.stderr = io.StringIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0


class _InspectableStdin(io.StringIO):
    def close(self) -> None:
        # The client should close stdin; retaining this in-memory buffer lets
        # the test assert the exact JSONL it wrote afterwards.
        self.flush()


def test_transport_initializes_and_matches_request_ids() -> None:
    process = FakeProcess(
        [
            {"id": 999, "result": {"ignored": True}},
            {"id": 0, "result": {"platformFamily": "linux"}},
            {"method": "account/updated", "params": {"authMode": "chatgpt"}},
            {"id": 1, "result": {"dailyUsageBuckets": []}},
        ]
    )
    with CodexAppServer(process_factory=lambda *_args, **_kwargs: process) as server:
        result = server.request("account/usage/read")
    assert result == {"dailyUsageBuckets": []}
    sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert sent[0]["method"] == "initialize"
    assert sent[0]["id"] == 0
    assert sent[1] == {"method": "initialized", "params": {}}
    assert sent[2]["method"] == "account/usage/read"
    assert sent[2]["id"] == 1


def test_transport_sets_codex_home_for_child(tmp_path: Path) -> None:
    process = FakeProcess([{"id": 0, "result": {}}])
    captured: dict = {}

    def factory(*_args, **kwargs):
        captured.update(kwargs)
        return process

    with CodexAppServer(codex_home=tmp_path / ".codex", process_factory=factory):
        pass
    assert captured["env"]["CODEX_HOME"] == str(tmp_path / ".codex")


def test_child_env_forwards_allowlist_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PLANTRACK_CLAUDE_SESSION_KEY", "super-secret-token")
    monkeypatch.setenv("PLANTRACK_CODEX_EXECUTABLE", "/trojan/codex")
    fake_path = os.pathsep.join(["/opt/bin", "/usr/bin"])
    monkeypatch.setenv("PATH", fake_path)
    # SYSTEMROOT always exists on Windows; set it elsewhere so the assertion
    # is meaningful on any platform while never fighting a real value.
    if "SYSTEMROOT" in os.environ:
        systemroot = os.environ["SYSTEMROOT"]
    else:
        systemroot = "C:\\Windows"
        monkeypatch.setenv("SYSTEMROOT", systemroot)

    env = _child_env(tmp_path / ".codex")
    assert env["PATH"] == fake_path
    assert env["SYSTEMROOT"] == systemroot
    assert env["CODEX_HOME"] == str(tmp_path / ".codex")
    # Secrets and parent-side configuration must never reach the child.
    assert "PLANTRACK_CLAUDE_SESSION_KEY" not in env
    assert "PLANTRACK_CODEX_EXECUTABLE" not in env


def test_child_env_omits_codex_home_when_unset() -> None:
    assert "CODEX_HOME" not in _child_env(None)


def test_device_login_shows_code_then_waits_for_matching_completion(
    monkeypatch, tmp_path: Path
) -> None:
    process = FakeProcess(
        [
            {"id": 0, "result": {}},
            {
                "id": 1,
                "result": {
                    "type": "chatgptDeviceCode",
                    "loginId": "login-1",
                    "verificationUrl": "https://auth.openai.com/codex/device",
                    "userCode": "ABCD-1234",
                },
            },
            {
                "method": "account/login/completed",
                "params": {"loginId": "other", "success": True},
            },
            {
                "method": "account/login/completed",
                "params": {"loginId": "login-1", "success": True},
            },
        ]
    )
    monkeypatch.setattr(codex, "CodexAppServer", fake_server_factory(process))
    shown: list[codex.DeviceLogin] = []
    login_home = tmp_path / "new-home"
    codex.login_with_device_code(shown.append, home=login_home, timeout=2)
    assert (login_home / ".codex").is_dir()
    assert shown == [
        codex.DeviceLogin("https://auth.openai.com/codex/device", "ABCD-1234", "login-1")
    ]


def test_rate_limit_mapping_supports_standard_and_arbitrary_windows() -> None:
    quotas = codex.quotas_from_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 25,
                        "windowDurationMins": 300,
                        "resetsAt": 1_700_000_000,
                    },
                    "secondary": {
                        "usedPercent": 70,
                        "windowDurationMins": 10080,
                        "resetsAt": 1_700_100_000,
                    },
                },
                "other": {
                    "limitId": "other",
                    "limitName": "Extra",
                    "primary": {"usedPercent": 50, "windowDurationMins": 90, "resetsAt": None},
                },
            }
        }
    )
    by_kind = {quota.kind: quota for quota in quotas}
    assert by_kind["5h"].remaining_percent == 75
    assert by_kind["weekly"].remaining_percent == 30
    assert by_kind["90m"].remaining_percent == 50
    assert by_kind["5h"].resets_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_rate_limit_mapping_prioritizes_the_codex_ui_meter() -> None:
    quotas = codex.quotas_from_rate_limits(
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 85, "windowDurationMins": 300, "resetsAt": 100},
                "secondary": {"usedPercent": 35, "windowDurationMins": 10080, "resetsAt": 200},
            },
            "rateLimitsByLimitId": {
                "base_model_inference": {
                    "limitId": "base_model_inference",
                    "limitName": "gpt-reserve",
                    "primary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 300},
                },
                "codex": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 85, "windowDurationMins": 300, "resetsAt": 100},
                    "secondary": {"usedPercent": 35, "windowDurationMins": 10080, "resetsAt": 200},
                },
            },
        }
    )

    by_kind = {quota.kind: quota for quota in quotas}
    assert by_kind["5h"].remaining_percent == 15
    assert by_kind["weekly"].remaining_percent == 65
    assert by_kind["gpt-reserve weekly"].remaining_percent == 100


def test_status_uses_live_codex_quotas(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        codex,
        "fetch_quotas",
        lambda _home: [QuotaWindow("5h", 80.0, datetime(2026, 8, 16, tzinfo=timezone.utc))],
    )
    status = {item.plan_id: item for item in collect_statuses(home)}["chatgpt-codex"]
    assert status.quotas[0].remaining_percent == 80.0
    assert status.note is None


def test_remote_usage_preferred_and_filtered(home: Path, monkeypatch) -> None:
    remote = [
        codex.UsageRecord(
            date=date(2026, 8, 15),
            source="codex-account",
            plan_id="chatgpt-codex",
            model="chatgpt-codex account total",
            input_tokens=123,
            requests=1,
        ),
        codex.UsageRecord(
            date=date(2026, 8, 14),
            source="codex-account",
            plan_id="chatgpt-codex",
            model="chatgpt-codex account total",
            input_tokens=9,
            requests=1,
        ),
    ]
    monkeypatch.setattr(codex, "fetch_remote_usage", lambda _home: remote)
    records = list(codex.iter_usage(home, since=date(2026, 8, 15)))
    assert records == [remote[0]]
    assert records[0].input_tokens == 123
    assert records[0].source == "codex-account"


def test_remote_bucket_parser_uses_total_as_input_tokens(monkeypatch, home: Path) -> None:
    process = FakeProcess(
        [
            {"id": 0, "result": {}},
            {
                "id": 1,
                "result": {
                    "summary": {},
                    "dailyUsageBuckets": [{"startDate": "2026-08-15", "tokens": 456}],
                },
            },
        ]
    )
    monkeypatch.setattr(codex, "CodexAppServer", fake_server_factory(process))
    records = fetch_remote_usage_unmocked(home)
    assert len(records) == 1
    assert records[0].input_tokens == 456
    assert records[0].output_tokens == 0
    assert records[0].requests == 0
