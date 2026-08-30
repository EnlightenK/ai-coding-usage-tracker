"""MiniMax Token Plan quota provider using the public remains endpoint."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from .. import payload_dump
from ..models import QuotaWindow
from ..parsing import ms_to_datetime

_INACTIVE_CODES = {2062}


@dataclass
class MiniMaxRemains:
    """Parsed quota windows from the MiniMax token plan remains API."""

    active: bool | None
    quotas: list[QuotaWindow] = field(default_factory=list)
    note: str | None = None


def parse_remains(payload: dict) -> MiniMaxRemains:
    """Parse a token_plan/remains JSON payload into quota windows."""
    base = payload.get("base_resp")
    status_code = base.get("status_code") if isinstance(base, dict) else None
    status_msg = base.get("status_msg") if isinstance(base, dict) else None

    if isinstance(status_code, int) and status_code != 0:
        # 2062 is the known "no active subscription" code; any other error is
        # an unknown state, not evidence of an active plan.
        active = False if status_code in _INACTIVE_CODES else None
        return MiniMaxRemains(
            active=active,
            note=f"API error {status_code}: {status_msg or 'unknown error'}",
        )

    model_remains = payload.get("model_remains")
    if not isinstance(model_remains, list) or not model_remains:
        return MiniMaxRemains(active=None, note="no quota data returned")

    quotas: list[QuotaWindow] = []
    seen_kinds: set[str] = set()
    for entry in model_remains:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("model_name")
        if kind != "general":
            continue
        interval = QuotaWindow(
            kind="5h",
            remaining_percent=_percent(entry.get("current_interval_remaining_percent")),
            resets_at=ms_to_datetime(entry.get("end_time")),
        )
        weekly = QuotaWindow(
            kind="weekly",
            remaining_percent=_percent(entry.get("current_weekly_remaining_percent")),
            resets_at=ms_to_datetime(entry.get("weekly_end_time")),
        )
        for window in (interval, weekly):
            if window.kind not in seen_kinds:
                quotas.append(window)
                seen_kinds.add(window.kind)

    if not quotas:
        return MiniMaxRemains(active=None, note="no usage-tracked models in plan")
    return MiniMaxRemains(active=True, quotas=quotas)


def _percent(value: object) -> float | None:
    if isinstance(value, (int, float)) and 0 <= value <= 100:
        return float(value)
    return None


_TRUSTED_HOSTNAMES = ("minimaxi.com", "minimax.io")


def _trusted_host(host: str) -> bool:
    """Defense in depth: only https MiniMax-owned hostnames may receive the key.

    Discovery already sanitizes hosts, but this module is the last line
    before the Bearer token leaves the machine, so it re-checks.
    """
    try:
        parts = urlsplit(host)
        hostname = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme != "https" or not hostname:
        return False
    return hostname in _TRUSTED_HOSTNAMES or hostname.endswith((".minimaxi.com", ".minimax.io"))


def fetch_remains(api_key: str, host: str, timeout: float = 15.0) -> MiniMaxRemains:
    """Call the MiniMax token plan remains endpoint for one subscription key."""
    if not _trusted_host(host):
        return MiniMaxRemains(
            active=None,
            note=f"refusing to send API key to untrusted host {host!r}",
        )
    url = host.rstrip("/") + "/v1/token_plan/remains"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return MiniMaxRemains(active=None, note=f"network error: {exc}")
    except ValueError:
        return MiniMaxRemains(active=None, note="invalid JSON response")
    if not isinstance(payload, dict):
        return MiniMaxRemains(active=None, note="unexpected response shape")
    payload_dump.dump(_dump_name(host), payload)
    return parse_remains(payload)


def _dump_name(host: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    return f"minimax-{suffix}-remains"
