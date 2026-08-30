"""Claude Code provider: OAuth subscription state and local transcript usage."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path

from .. import paths
from ..models import QuotaWindow, SubscriptionInfo, UsageRecord
from ..parsing import ms_to_datetime, parse_iso, positive_int, read_json_dict
from . import claude_limits as _limits
from . import claude_profile as _profile


def cached_quotas(home: Path | None = None) -> list[QuotaWindow]:
    """Return Claude rate-limit windows captured from the statusline feed."""
    snapshot = _limits.load_cached(home)
    if snapshot is None:
        return []
    quotas: list[QuotaWindow] = []
    mapping = {"five_hour": "5h", "seven_day": "weekly"}
    for source_kind, kind in mapping.items():
        window = snapshot.get(source_kind)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percentage")
        resets_at = window.get("resets_at")
        resets = None
        if isinstance(resets_at, (int, float)):
            try:
                resets = datetime.fromtimestamp(resets_at, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                # Hostile or absurd epoch values must not crash the whole run.
                resets = None
        remaining = 100.0 - used if isinstance(used, (int, float)) else None
        quotas.append(QuotaWindow(kind=kind, remaining_percent=remaining, resets_at=resets))
    return quotas


def plan_for_model(model: str) -> str:
    """Map a Claude transcript model name to the plan it consumes quota from."""
    lowered = model.lower()
    if lowered.startswith("glm"):
        return "glm-intl"
    if "minimax" in lowered:
        return "minimax-cn"
    return "claude-code"


def subscription_status(home: Path | None = None) -> SubscriptionInfo | None:
    """Return the Claude subscription state (see `subscription_state`)."""
    return subscription_state(home)[0]


def subscription_state(
    home: Path | None = None,
) -> tuple[SubscriptionInfo | None, str | None]:
    """Report the Claude subscription, from the account profile where possible.

    The plan tier, billing channel and subscription status come from the
    claude.ai account profile; the local credentials only contribute the
    OAuth lifetime, which is an auth detail and never a billing countdown.

    Returns (subscription, note).
    """
    home = home or paths.default_home()
    credentials = read_json_dict(paths.claude_dir(home) / ".credentials.json")
    if not credentials:
        return None, None
    oauth = credentials.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None, None
    now = datetime.now(tz=timezone.utc)
    auth_valid_until = ms_to_datetime(oauth.get("refreshTokenExpiresAt"))
    auth_days_left = (auth_valid_until - now).total_seconds() / 86400 if auth_valid_until else None
    local_plan = oauth.get("subscriptionType")

    payload, note = _profile.load_profile(home)
    profile = _profile.parse_profile(payload)
    # A trial end date is the one genuine subscription countdown Anthropic
    # exposes; ordinary plans report no end date at all.
    valid_until = profile.trial_ends_at
    days_left = (valid_until - now).total_seconds() / 86400 if valid_until else None
    return (
        SubscriptionInfo(
            plan_type=profile.plan_type or (local_plan if isinstance(local_plan, str) else None),
            valid_until=valid_until,
            days_left=days_left,
            email=profile.email,
            status=profile.status,
            billing=profile.billing,
            source="claude.ai profile" if payload else "local credentials",
            auth_valid_until=auth_valid_until,
            auth_days_left=auth_days_left,
        ),
        note,
    )


def iter_usage(home: Path | None = None, since: date | None = None) -> Iterator[UsageRecord]:
    """Yield per-message usage records from Claude Code project transcripts."""
    home = home or paths.default_home()
    projects_dir = paths.claude_dir(home) / "projects"
    if not projects_dir.is_dir():
        return
    seen_ids: set[str] = set()
    for file in sorted(projects_dir.rglob("*.jsonl")):
        yield from _parse_transcript(file, since, seen_ids)


def _parse_transcript(file: Path, since: date | None, seen_ids: set[str]) -> Iterator[UsageRecord]:
    # Streamed line by line: transcripts can be large, and reading them whole
    # would hold the entire file in memory at once.
    try:
        with file.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if isinstance(message_id, str):
                    if message_id in seen_ids:
                        continue
                    seen_ids.add(message_id)
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                timestamp = parse_iso(entry.get("timestamp"))
                if since is not None and timestamp is not None:
                    if timestamp.date() < since:
                        continue
                day = timestamp.date() if timestamp else date.min
                model = message.get("model")
                yield UsageRecord(
                    date=day,
                    source="claude-code",
                    plan_id=plan_for_model(model if isinstance(model, str) else "unknown"),
                    model=model if isinstance(model, str) else "unknown",
                    input_tokens=positive_int(usage.get("input_tokens")),
                    output_tokens=positive_int(usage.get("output_tokens")),
                    cache_read_tokens=positive_int(usage.get("cache_read_input_tokens")),
                    cache_write_tokens=positive_int(usage.get("cache_creation_input_tokens")),
                    requests=1,
                )
    except OSError:
        return
