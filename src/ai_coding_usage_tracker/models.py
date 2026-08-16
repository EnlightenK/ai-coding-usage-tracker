"""Shared data models for the AI coding plan usage tracker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class QuotaWindow:
    """Remaining quota for one limiting window, e.g. a 5-hour or weekly window."""

    kind: str
    remaining_percent: float | None
    resets_at: datetime | None


@dataclass(frozen=True)
class SubscriptionInfo:
    """Subscription details attached to a discovered plan.

    `valid_until` / `days_left` describe the *subscription* itself and are only
    set when the provider reports a real end date. Credential lifetimes live in
    `auth_valid_until` / `auth_days_left`, which say nothing about billing.
    """

    plan_type: str | None
    valid_until: datetime | None
    days_left: float | None
    email: str | None = None
    status: str | None = None
    billing: str | None = None
    source: str | None = None
    auth_valid_until: datetime | None = None
    auth_days_left: float | None = None


@dataclass
class PlanStatus:
    """Current state of one AI coding plan subscription."""

    plan_id: str
    provider: str
    name: str
    region: str | None
    auth_kind: str
    configured: bool
    active: bool | None
    subscription: SubscriptionInfo | None = None
    quotas: list[QuotaWindow] = field(default_factory=list)
    note: str | None = None


@dataclass(frozen=True)
class UsageRecord:
    """Aggregated token usage for one day, source, plan and model."""

    date: date
    source: str
    plan_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )
