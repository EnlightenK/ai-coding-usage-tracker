"""Claude account profile: real subscription state from the oauth profile API.

The Claude Code OAuth token is already scoped for `user:profile`, so the same
endpoint the desktop app uses returns the account's plan tier, subscription
status and billing channel without any extra credential. The claude.ai session
key is accepted as a fallback for machines where the OAuth token has lapsed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .. import fileutil, paths
from ..parsing import now_ms, parse_iso, read_json_dict
from .claude_limits import PROFILE_URL, load_session_key

CACHE_FILENAME = "claude-profile.json"
MAX_AGE = timedelta(hours=12)

# Statuses worth showing to a user: they mean the plan may stop working.
PROBLEM_STATUSES = frozenset(
    {"canceled", "cancelled", "past_due", "unpaid", "expired", "paused"}
)
# Billing handled by an app store: Anthropic's own subscription_status is a
# Stripe artifact there ('incomplete' is normal) and must not be alarmed on.
EXTERNAL_BILLING = frozenset(
    {"apple_subscription", "google_play_subscription", "play_subscription"}
)


@dataclass(frozen=True)
class ProfileInfo:
    """Subscription facts parsed out of an oauth profile payload."""

    plan_type: str | None = None
    status: str | None = None
    billing: str | None = None
    email: str | None = None
    trial_ends_at: datetime | None = None

    @property
    def status_is_concerning(self) -> bool:
        """Whether `status` signals a real billing problem worth surfacing."""
        return status_is_concerning(self.status, self.billing)


def status_is_concerning(status: str | None, billing: str | None) -> bool:
    """Whether a subscription status means the plan may actually stop working.

    App-store billing is excluded: Anthropic's own subscription record stays
    'incomplete' for those accounts because Apple or Google collects payment,
    so the value says nothing about whether the plan is healthy.
    """
    if not status or status.lower() not in PROBLEM_STATUSES:
        return False
    return (billing or "").lower() not in EXTERNAL_BILLING


def cache_file(home: Path | None = None) -> Path:
    """Return the path of the Claude account profile cache file."""
    home = home or paths.default_home()
    return paths.ptk_data_dir(home) / CACHE_FILENAME


def load_cached(
    home: Path | None = None, max_age: timedelta | None = MAX_AGE
) -> dict | None:
    """Load a cached profile payload, optionally requiring it to be fresh."""
    try:
        snapshot = json.loads(cache_file(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None
    profile = snapshot.get("profile")
    if not isinstance(profile, dict):
        return None
    if max_age is None:
        return profile
    fetched_at = parse_iso(snapshot.get("fetched_at"))
    if fetched_at is None:
        return None
    if datetime.now(tz=timezone.utc) - fetched_at > max_age:
        return None
    return profile


def load_profile(
    home: Path | None = None, timeout: float = 15.0
) -> tuple[dict | None, str | None]:
    """Return the account profile, preferring a fresh cache over the network.

    Falls back to a stale cache when the fetch fails, so an offline run keeps
    reporting the last known subscription state instead of nothing.

    Returns (profile, note).
    """
    home = home or paths.default_home()
    cached = load_cached(home)
    if cached is not None:
        return cached, None
    profile, note = fetch_profile(home, timeout)
    if profile is not None:
        return profile, note
    stale = load_cached(home, max_age=None)
    if stale is not None:
        return stale, f"using last known subscription state ({note})"
    return None, note


def fetch_profile(
    home: Path | None = None, timeout: float = 15.0
) -> tuple[dict | None, str | None]:
    """Fetch the account profile and cache it. Returns (profile, note)."""
    home = home or paths.default_home()
    tokens = _auth_tokens(home)
    if not tokens:
        return None, "no Claude credentials found"
    note: str | None = None
    for token, source in tokens:
        try:
            response = requests.get(
                PROFILE_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            note = f"network error: {exc}"
            continue
        if response.status_code != 200:
            note = f"{source} rejected (HTTP {response.status_code})"
            continue
        try:
            payload = response.json()
        except ValueError:
            note = f"{source} returned a non-JSON profile"
            continue
        if not isinstance(payload, dict):
            note = f"{source} returned an unexpected profile shape"
            continue
        _write_cache(payload, home)
        return payload, None
    return None, note


def parse_profile(profile: dict | None) -> ProfileInfo:
    """Pull the subscription facts out of an oauth profile payload."""
    if not isinstance(profile, dict):
        return ProfileInfo()
    account = profile.get("account")
    account = account if isinstance(account, dict) else {}
    organization = profile.get("organization")
    organization = organization if isinstance(organization, dict) else {}
    return ProfileInfo(
        plan_type=_plan_type(account, organization),
        status=_str(organization.get("subscription_status")),
        billing=_str(organization.get("billing_type")),
        email=_str(account.get("email")),
        trial_ends_at=parse_iso(organization.get("claude_code_trial_ends_at")),
    )


def _plan_type(account: dict, organization: dict) -> str | None:
    """Name the plan tier, preferring the account flags over the org type."""
    if account.get("has_claude_max") is True:
        return "max"
    if account.get("has_claude_pro") is True:
        return "pro"
    org_type = _str(organization.get("organization_type"))
    if org_type:
        return org_type.removeprefix("claude_").replace("_", " ")
    seat = _str(organization.get("seat_tier"))
    return seat or None


def _auth_tokens(home: Path) -> list[tuple[str, str]]:
    """Return usable bearer tokens, best first: OAuth token, then session key."""
    tokens: list[tuple[str, str]] = []
    oauth = _read_oauth(home)
    if oauth:
        access = oauth.get("accessToken")
        expires = oauth.get("expiresAt")
        if isinstance(access, str) and access:
            fresh = not isinstance(expires, (int, float)) or expires > now_ms()
            if fresh:
                tokens.append((access, "Claude Code OAuth token"))
    session_key = load_session_key(home)
    if session_key:
        tokens.append((session_key, "claude.ai session key"))
    return tokens


def _read_oauth(home: Path) -> dict | None:
    data = read_json_dict(paths.claude_dir(home) / ".credentials.json")
    oauth = data.get("claudeAiOauth") if data else None
    return oauth if isinstance(oauth, dict) else None


def _write_cache(profile: dict, home: Path | None) -> bool:
    target = cache_file(home)
    snapshot = {
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "profile": profile,
    }
    fileutil.secure_dir(target.parent)
    return fileutil.secure_write_text(target, json.dumps(snapshot))


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
