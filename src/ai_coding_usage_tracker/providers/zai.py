"""Z.ai GLM Coding Plan quota provider using the monitor usage API."""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .. import payload_dump
from ..models import QuotaWindow, SubscriptionInfo
from ..parsing import ms_to_datetime

QUOTA_LIMIT_URL = "https://api.z.ai/api/monitor/usage/quota/limit"

# (unit, number) pairs the API uses to identify each limiting window.
_FIVE_HOUR = (3, 5)
_WEEKLY = (6, 1)


@dataclass
class ZaiQuota:
    """Parsed quota windows and plan level from the Z.ai monitor API."""

    active: bool | None
    quotas: list[QuotaWindow] = field(default_factory=list)
    subscription: SubscriptionInfo | None = None
    note: str | None = None


def parse_limits(payload: dict) -> ZaiQuota:
    """Parse a quota/limit response into quota windows."""
    if not isinstance(payload.get("success"), bool) or not payload["success"]:
        msg = payload.get("msg")
        return ZaiQuota(active=None, note=f"API error: {msg or 'unsuccessful response'}")

    data = payload.get("data")
    if not isinstance(data, dict):
        return ZaiQuota(active=None, note="no data in response")

    quotas: list[QuotaWindow] = []
    limits = data.get("limits")
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict) or limit.get("type") != "TOKENS_LIMIT":
            continue
        key = (limit.get("unit"), limit.get("number"))
        kind = {_FIVE_HOUR: "5h", _WEEKLY: "weekly"}.get(key)
        if kind is None:
            continue
        used = limit.get("percentage")
        remaining = 100.0 - used if isinstance(used, (int, float)) else None
        quotas.append(
            QuotaWindow(
                kind=kind,
                remaining_percent=remaining,
                resets_at=ms_to_datetime(limit.get("nextResetTime")),
            )
        )

    level = data.get("level")
    subscription = None
    if isinstance(level, str) and level:
        subscription = SubscriptionInfo(plan_type=level, valid_until=None, days_left=None)
    if not quotas:
        return ZaiQuota(
            active=None, subscription=subscription, note="no token limits in response"
        )
    return ZaiQuota(active=True, quotas=quotas, subscription=subscription)


def fetch_limits(api_key: str, timeout: float = 15.0) -> ZaiQuota:
    """Query the Z.ai monitor quota limit endpoint for a coding plan key."""
    try:
        response = requests.get(
            QUOTA_LIMIT_URL,
            headers={"Authorization": api_key, "Accept-Language": "en-US,en"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return ZaiQuota(active=None, note=f"network error: {exc}")
    if response.status_code in (401, 403):
        return ZaiQuota(active=False, note="authentication rejected")
    if response.status_code != 200:
        return ZaiQuota(active=None, note=f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return ZaiQuota(active=None, note="invalid JSON response")
    if not isinstance(payload, dict):
        return ZaiQuota(active=None, note="unexpected response shape")
    payload_dump.dump("zai-quota-limit", payload)
    return parse_limits(payload)
