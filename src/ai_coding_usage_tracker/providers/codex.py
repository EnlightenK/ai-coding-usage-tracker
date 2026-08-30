"""Codex ChatGPT-plan status and usage, via app-server or local sessions."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .. import paths, payload_dump
from ..models import QuotaWindow, SubscriptionInfo, UsageRecord
from ..parsing import parse_iso, positive_int, read_json_dict
from .codex_app_server import CodexAppServer, CodexAppServerError

# Codex usage reaches the tracker through one of two mutually-exclusive
# channels, never both at once: account-wide daily totals from the app-server,
# or locally parsed rollout session logs (see `iter_usage`).  The store keys
# its de-duplication of superseded rows on these names.
SESSION_SOURCE = "codex"
ACCOUNT_SOURCE = "codex-account"


@dataclass(frozen=True)
class DeviceLogin:
    """Non-sensitive details needed to complete a headless ChatGPT login."""

    verification_url: str
    user_code: str
    login_id: str


def decode_jwt_payload(token: str) -> dict | None:
    """Decode the payload segment of a JWT without verifying its signature.

    SECURITY: the returned payload is NOT signature-verified.  It is read
    only from the local ``~/.codex/auth.json`` written by the Codex CLI
    itself, and must never be used for authorization decisions; it is for
    display only.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment.encode("ascii"))
        payload = json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def plan_for_model_provider(provider: object) -> str:
    """Map a Codex model provider name to the plan it consumes quota from."""
    if isinstance(provider, str) and "minimax" in provider.lower():
        return "minimax-intl"
    return "chatgpt-codex"


def subscription_status(home: Path | None = None) -> SubscriptionInfo | None:
    """Read plan type and validity from the Codex OAuth id_token claims."""
    home = home or paths.default_home()
    auth = read_json_dict(paths.codex_dir(home) / "auth.json")
    if not auth:
        return None
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        return None
    id_token = tokens.get("id_token")
    claims = decode_jwt_payload(id_token) if isinstance(id_token, str) else None
    if not claims:
        return None
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    now = datetime.now(tz=timezone.utc)
    valid_until = _parse_timestamp(auth_claims.get("chatgpt_subscription_active_until"))
    days_left = (valid_until - now).total_seconds() / 86400 if valid_until else None
    plan_type = auth_claims.get("chatgpt_plan_type")
    email = claims.get("email")
    return SubscriptionInfo(
        plan_type=plan_type if isinstance(plan_type, str) else None,
        valid_until=valid_until,
        days_left=days_left,
        email=email if isinstance(email, str) else None,
    )


def has_chatgpt_auth(home: Path | None = None) -> bool:
    """Whether this home has a Codex-managed ChatGPT login for app-server."""
    home = home or paths.default_home()
    auth = read_json_dict(paths.codex_dir(home) / "auth.json")
    return bool(auth and auth.get("auth_mode") == "chatgpt")


def fetch_quotas(home: Path | None = None) -> list[QuotaWindow]:
    """Read live ChatGPT Codex limits through the local app-server client."""
    home = home or paths.default_home()
    if not has_chatgpt_auth(home):
        raise CodexAppServerError("No ChatGPT Codex login is configured")
    with CodexAppServer(codex_home=paths.codex_dir(home)) as server:
        payload = server.request("account/rateLimits/read")
    payload_dump.dump("codex-rate-limits", payload)
    return quotas_from_rate_limits(payload)


def quotas_from_rate_limits(payload: dict[str, Any]) -> list[QuotaWindow]:
    """Convert the stable app-server rate-limit response into tracker windows."""
    buckets = payload.get("rateLimitsByLimitId")
    entries: list[tuple[str | None, dict[str, Any]]] = []
    if isinstance(buckets, dict):
        for limit_id, value in buckets.items():
            if isinstance(value, dict):
                entries.append((str(limit_id), value))
    if not entries and isinstance(payload.get("rateLimits"), dict):
        entries.append((None, payload["rateLimits"]))

    quotas: list[QuotaWindow] = []
    used_kinds: set[str] = set()
    for fallback_id, bucket in entries:
        limit_id = bucket.get("limitId")
        label = bucket.get("limitName")
        prefix = label if isinstance(label, str) and label else limit_id
        if not isinstance(prefix, str) or not prefix:
            prefix = fallback_id
        for side in ("primary", "secondary"):
            window = bucket.get(side)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            if not isinstance(used, (int, float)) or isinstance(used, bool):
                continue
            duration = window.get("windowDurationMins")
            duration_mins = duration if isinstance(duration, int) and duration > 0 else None
            kind = _quota_kind(duration_mins, side)
            # Keep familiar labels when unambiguous.  Multi-metered accounts
            # retain every window using a stable, descriptive prefixed label.
            if kind in used_kinds:
                kind = f"{prefix or 'codex'} {kind}"
            used_kinds.add(kind)
            quotas.append(
                QuotaWindow(
                    kind=kind,
                    remaining_percent=max(0.0, min(100.0, 100.0 - float(used))),
                    resets_at=_parse_timestamp(window.get("resetsAt")),
                )
            )
    return quotas


def fetch_remote_usage(home: Path | None = None) -> list[UsageRecord]:
    """Read account-wide Codex daily token buckets from app-server.

    The service provides a total only, so it is represented as input tokens and
    intentionally uses a distinct source/model from local session details.
    """
    home = home or paths.default_home()
    if not has_chatgpt_auth(home):
        raise CodexAppServerError("No ChatGPT Codex login is configured")
    with CodexAppServer(codex_home=paths.codex_dir(home)) as server:
        payload = server.request("account/usage/read")
    payload_dump.dump("codex-usage", payload)
    buckets = payload.get("dailyUsageBuckets")
    if buckets is None:
        raise CodexAppServerError("Codex did not provide daily usage buckets")
    if not isinstance(buckets, list):
        raise CodexAppServerError("Codex returned an invalid daily usage bucket list")
    records: list[UsageRecord] = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        start_date, tokens = bucket.get("startDate"), bucket.get("tokens")
        if not isinstance(start_date, str) or not isinstance(tokens, int):
            continue
        if isinstance(tokens, bool):
            continue
        try:
            day = date.fromisoformat(start_date)
        except ValueError:
            continue
        records.append(
            UsageRecord(
                date=day,
                source=ACCOUNT_SOURCE,
                plan_id="chatgpt-codex",
                model="chatgpt-codex account total",
                input_tokens=max(0, tokens),
                # account/usage/read exposes token totals, not request counts.
                requests=0,
            )
        )
    return records


def login_with_device_code(
    show_code: Callable[[DeviceLogin], None],
    *,
    home: Path | None = None,
    timeout: float = 600.0,
) -> None:
    """Run ChatGPT device login and invoke ``show_code`` before waiting.

    Only the verification URL and user code are exposed to the caller; OAuth
    tokens remain owned by the Codex CLI in its private configuration directory.
    """
    home = home or paths.default_home()
    codex_home = paths.codex_dir(home)
    codex_home.mkdir(parents=True, exist_ok=True)
    with CodexAppServer(
        codex_home=codex_home, timeout=min(timeout, 30.0)
    ) as server:
        result = server.request("account/login/start", {"type": "chatgptDeviceCode"})
        verification_url = result.get("verificationUrl")
        user_code = result.get("userCode")
        login_id = result.get("loginId")
        details = (verification_url, user_code, login_id)
        if not all(isinstance(value, str) and value for value in details):
            raise CodexAppServerError("Codex returned an invalid device-login response")
        show_code(DeviceLogin(verification_url, user_code, login_id))
        completed = server.wait_for_notification(
            "account/login/completed",
            timeout=timeout,
            predicate=lambda message: isinstance(message.get("params"), dict)
            and message["params"].get("loginId") == login_id,
        )
    params = completed.get("params")
    if not isinstance(params, dict) or params.get("success") is not True:
        error = params.get("error") if isinstance(params, dict) else None
        suffix = f": {error}" if isinstance(error, str) and error else ""
        raise CodexAppServerError(f"ChatGPT device login did not complete{suffix}")


def iter_usage(home: Path | None = None, since: date | None = None) -> Iterator[UsageRecord]:
    """Yield remote account buckets, falling back to local Codex session logs."""
    home = home or paths.default_home()
    if has_chatgpt_auth(home):
        try:
            remote = fetch_remote_usage(home)
        except CodexAppServerError:
            remote = None
        if remote is not None:
            for record in remote:
                if since is None or record.date >= since:
                    yield record
            return
    yield from iter_local_usage(home, since)


def iter_local_usage(home: Path | None = None, since: date | None = None) -> Iterator[UsageRecord]:
    """Yield per-session usage records from local Codex rollout session logs."""
    home = home or paths.default_home()
    codex = paths.codex_dir(home)
    files = list((codex / "sessions").rglob("*.jsonl"))
    archived = codex / "archived_sessions"
    if archived.is_dir():
        files.extend(archived.glob("*.jsonl"))
    seen_paths: set[Path] = set()
    for file in sorted(files):
        if file in seen_paths:
            continue
        seen_paths.add(file)
        record = _parse_session(file, since)
        if record is not None and record.requests > 0:
            yield record


def _quota_kind(duration_mins: int | None, side: str) -> str:
    if duration_mins == 300:
        return "5h"
    if duration_mins == 7 * 24 * 60:
        return "weekly"
    if duration_mins is None:
        return side
    if duration_mins % (24 * 60) == 0:
        return f"{duration_mins // (24 * 60)}d"
    if duration_mins % 60 == 0:
        return f"{duration_mins // 60}h"
    return f"{duration_mins}m"


def _parse_session(file: Path, since: date | None) -> UsageRecord | None:
    first_timestamp: datetime | None = None
    model_provider: object = None
    model_name: str | None = None
    total_usage: dict | None = None
    token_events = 0
    # Streamed line by line: rollout session logs can be large, and reading
    # them whole would hold the entire file in memory at once.
    try:
        with file.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                timestamp = _parse_timestamp(entry.get("timestamp"))
                if timestamp is not None and first_timestamp is None:
                    first_timestamp = timestamp
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict) and isinstance(
                        info.get("total_token_usage"), dict
                    ):
                        total_usage = info["total_token_usage"]
                        token_events += 1
                elif entry.get("type") == "session_meta":
                    model_provider = payload.get("model_provider")
                elif payload.get("type") == "turn_context":
                    model = payload.get("model")
                    if isinstance(model, str) and model:
                        model_name = model
    except OSError:
        return None
    if total_usage is None:
        return None
    day = first_timestamp.date() if first_timestamp else date.min
    if since is not None and day < since:
        return None
    input_tokens = positive_int(total_usage.get("input_tokens"))
    output_tokens = positive_int(total_usage.get("output_tokens"))
    reasoning_tokens = positive_int(total_usage.get("reasoning_output_tokens"))
    cache_read_tokens = positive_int(total_usage.get("cached_input_tokens"))
    cache_write_tokens = positive_int(total_usage.get("cache_write_input_tokens"))
    components = (
        input_tokens
        + output_tokens
        + reasoning_tokens
        + cache_read_tokens
        + cache_write_tokens
    )
    total = positive_int(total_usage.get("total_tokens"))
    if total > components:
        input_tokens += total - components
    return UsageRecord(
        date=day,
        source=SESSION_SOURCE,
        plan_id=plan_for_model_provider(model_provider),
        model=model_name or "codex",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        requests=token_events,
    )


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, str):
        return parse_iso(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None
