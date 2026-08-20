"""Collection of plan statuses across remote APIs and local OAuth state."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import paths, store
from .discovery import DiscoveredPlan, discover_plans
from .models import PlanStatus, QuotaWindow, SubscriptionInfo
from .parsing import parse_iso
from .providers import claude, claude_limits, claude_profile, codex, minimax, zai

DEFAULT_CACHE_TTL = 300.0


def collect_statuses(
    home: Path | None = None, *, max_cache_age: float = DEFAULT_CACHE_TTL, refresh: bool = False
) -> list[PlanStatus]:
    """Build the current status for every discovered plan, checked in parallel.

    Fresh results from the last `max_cache_age` seconds are served from the
    local store; `refresh` bypasses the cache read (a fresh fetch still
    updates both the cache and the status snapshots).
    """
    home = home or paths.default_home()
    plans = discover_plans(home)
    results: list[PlanStatus | None] = [None] * len(plans)
    if not refresh:
        for index, plan in enumerate(plans):
            hit = store.cached_status(home, plan.plan_id, max_cache_age)
            if hit is None:
                continue
            payload, stored_at = hit
            cached = _status_from_payload(payload)
            if cached is None:
                continue
            age = _age_text(datetime.now(tz=timezone.utc) - stored_at)
            cached.note = f"{cached.note}; cached {age} ago" if cached.note else f"cached {age} ago"
            results[index] = cached
    missing = [plan for plan, result in zip(plans, results, strict=True) if result is None]
    if missing:
        with ThreadPoolExecutor(max_workers=min(len(missing), 8)) as pool:
            fetched = list(pool.map(lambda plan: _status_for_plan(plan, home), missing))
        store.record_status(home, fetched)
        fetched_by_id = {status.plan_id: status for status in fetched}
        for index, plan in enumerate(plans):
            if results[index] is None and plan.plan_id in fetched_by_id:
                results[index] = fetched_by_id[plan.plan_id]
    return [status for status in results if status is not None]


def _age_text(delta: datetime) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, hours = seconds % 3600 // 60, seconds // 3600
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _status_from_payload(payload: dict) -> PlanStatus | None:
    """Rebuild a PlanStatus from its cached JSON payload, else None."""
    try:
        subscription = payload.get("subscription")
        if isinstance(subscription, dict):
            subscription = SubscriptionInfo(
                plan_type=subscription.get("plan_type"),
                valid_until=parse_iso(subscription.get("valid_until")),
                days_left=subscription.get("days_left"),
                email=subscription.get("email"),
                status=subscription.get("status"),
                billing=subscription.get("billing"),
                source=subscription.get("source"),
                auth_valid_until=parse_iso(subscription.get("auth_valid_until")),
                auth_days_left=subscription.get("auth_days_left"),
            )
        quotas = [
            QuotaWindow(
                kind=quota.get("kind") or "",
                remaining_percent=quota.get("remaining_percent"),
                resets_at=parse_iso(quota.get("resets_at")),
            )
            for quota in payload.get("quotas") or []
            if isinstance(quota, dict)
        ]
        return PlanStatus(
            plan_id=payload["plan_id"],
            provider=payload["provider"],
            name=payload["name"],
            region=payload.get("region"),
            auth_kind=payload["auth_kind"],
            configured=bool(payload.get("configured")),
            active=payload.get("active"),
            subscription=subscription,  # type: ignore[arg-type]
            quotas=quotas,
            note=payload.get("note"),
        )
    except (KeyError, TypeError, AttributeError):
        return None


def _status_for_plan(plan: DiscoveredPlan, home: Path) -> PlanStatus:
    configured = bool(plan.key_sources)
    if plan.plan_id in ("minimax-cn", "minimax-intl"):
        return _minimax_status(plan, configured)
    if plan.plan_id == "glm-intl":
        if not configured or not plan.api_key:
            return PlanStatus(
                plan_id=plan.plan_id,
                provider=plan.provider,
                name=plan.name,
                region=plan.region,
                auth_kind=plan.auth_kind,
                configured=False,
                active=None,
                note="not configured",
            )
        quota = zai.fetch_limits(plan.api_key)
        return PlanStatus(
            plan_id=plan.plan_id,
            provider=plan.provider,
            name=plan.name,
            region=plan.region,
            auth_kind=plan.auth_kind,
            configured=True,
            active=quota.active,
            subscription=quota.subscription,
            quotas=quota.quotas,
            note=quota.note,
        )
    if plan.plan_id == "claude-code":
        subscription, subscription_note = claude.subscription_state(home)
        quotas = claude.cached_quotas(home)
        age = claude_limits.captured_age(home)
        if quotas:
            note = f"rate limits as of {age} ago ({claude_limits.captured_source(home)})"
        elif claude_limits.load_session_key(home):
            success, refresh_note = claude_limits.refresh_from_api(home)
            if success:
                quotas = claude.cached_quotas(home)
                note = "rate limits refreshed via claude.ai session"
            else:
                age = claude_limits.captured_age(home)
                note = f"session refresh failed: {refresh_note}"
        elif age is not None:
            note = f"stale capture ({age} ago); open a Claude Code session to refresh"
        else:
            note = "rate limits appear once the statusline capture runs in a session"
        subscription_note = _claude_subscription_note(subscription, subscription_note)
        note = "; ".join(part for part in (note, subscription_note) if part)
        return PlanStatus(
            plan_id=plan.plan_id,
            provider=plan.provider,
            name=plan.name,
            region=plan.region,
            auth_kind=plan.auth_kind,
            configured=configured,
            active=True if configured else None,
            subscription=subscription,
            quotas=quotas,
            note=note,
        )
    if plan.plan_id == "chatgpt-codex":
        subscription = codex.subscription_status(home)
        quotas = []
        note = None
        if configured:
            try:
                quotas = codex.fetch_quotas(home)
                if not quotas:
                    note = "Codex returned no active quota windows"
            except codex.CodexAppServerError as exc:
                # Subscription claims remain useful on a VPS even when the CLI
                # is not installed yet or a login refresh needs attention.
                note = f"live Codex limits unavailable: {exc}"
        return PlanStatus(
            plan_id=plan.plan_id,
            provider=plan.provider,
            name=plan.name,
            region=plan.region,
            auth_kind=plan.auth_kind,
            configured=configured,
            active=True if configured else None,
            subscription=subscription,
            quotas=quotas,
            note=note,
        )
    raise ValueError(f"unknown plan: {plan.plan_id}")


def _claude_subscription_note(
    subscription: SubscriptionInfo | None, profile_note: str | None
) -> str | None:
    """Explain anything about the Claude subscription the table cannot show."""
    parts: list[str] = []
    if subscription is not None:
        if subscription.valid_until is not None:
            parts.append(
                f"Claude Code trial ends {subscription.valid_until.date().isoformat()}"
            )
        if claude_profile.status_is_concerning(
            subscription.status, subscription.billing
        ):
            parts.append(f"subscription {subscription.status}")
        elif subscription.source == "local credentials":
            parts.append("plan tier from local credentials (profile unavailable)")
    if profile_note:
        parts.append(profile_note)
    return "; ".join(parts) or None


def _minimax_status(plan: DiscoveredPlan, configured: bool) -> PlanStatus:
    if not configured or not plan.api_key:
        return PlanStatus(
            plan_id=plan.plan_id,
            provider=plan.provider,
            name=plan.name,
            region=plan.region,
            auth_kind=plan.auth_kind,
            configured=False,
            active=None,
            note="not configured",
        )
    remains = minimax.fetch_remains(plan.api_key, plan.api_host or "")
    return PlanStatus(
        plan_id=plan.plan_id,
        provider=plan.provider,
        name=plan.name,
        region=plan.region,
        auth_kind=plan.auth_kind,
        configured=True,
        active=remains.active,
        quotas=remains.quotas,
        note=remains.note,
    )
