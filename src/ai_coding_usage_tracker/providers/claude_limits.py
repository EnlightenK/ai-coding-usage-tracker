"""Claude rate limits: statusline capture, account-session refresh, cache."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .. import config, fileutil, paths, payload_dump
from ..parsing import age_text, now_ms, parse_iso, read_json_dict

CACHE_FILENAME = "claude-rate-limits.json"
SESSION_KEY_FILENAME = "session-key"
SESSION_KEY_ENV = "PLANTRACK_CLAUDE_SESSION_KEY"
SESSION_KEY_FILE_ENV = "PLANTRACK_SESSION_KEY_FILE"
MAX_AGE = timedelta(hours=6)
# Which channel produced a snapshot. Both write the same shape, so the label is
# recorded rather than inferred - a scheduled API refresh must not be reported
# as a statusline capture.
SOURCE_STATUSLINE = "statusline capture"
SOURCE_ACCOUNT_SESSION = "claude.ai session"
API_BASE = "https://api.anthropic.com/api"
PROFILE_URL = API_BASE + "/oauth/profile"

WINDOW_KEYS = ("five_hour", "seven_day")


def session_key_file(home: Path | None = None) -> Path:
    """Where the claude.ai session-key file is read from.

    PLANTRACK_SESSION_KEY_FILE wins; then a `session_key_file` entry in the
    plantrack config; otherwise ~/.local/ptk/session-key. Nothing points into
    a repository checkout unless explicitly configured.
    """
    home = home or paths.default_home()
    override = os.environ.get(SESSION_KEY_FILE_ENV)
    if override:
        # expanduser: env overrides are common in systemd/cron/.env contexts
        # where the shell does not expand ~ itself.
        return Path(override).expanduser()
    configured = config.load_config(home).get("session_key_file")
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()).expanduser()
    return paths.ptk_data_dir(home) / SESSION_KEY_FILENAME


def cache_file(home: Path | None = None) -> Path:
    """Return the path of the Claude rate limits cache file."""
    home = home or paths.default_home()
    return paths.ptk_data_dir(home) / CACHE_FILENAME


def load_session_key(home: Path | None = None) -> str | None:
    """Load the claude.ai account session key from env var or key file.

    The key is the `sessionKeyV3` cookie value from a logged-in claude.ai
    browser session. It is stored in PLANTRACK_CLAUDE_SESSION_KEY or in
    the session-key file (see `session_key_file`).
    """
    from_env = os.environ.get(SESSION_KEY_ENV)
    if from_env and from_env.strip():
        return from_env.strip()
    try:
        content = session_key_file(home).read_text(encoding="utf-8")
    except OSError:
        return None
    content = content.strip()
    return content or None


def capture_from_statusline_json(payload: dict, home: Path | None = None) -> bool:
    """Extract rate limits from statusline JSON and persist them.

    Returns True when the payload contained usable rate limit data.
    """
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return False
    return capture_windows(
        {k: v for k, v in rate_limits.items() if isinstance(v, dict)}, home
    )


def capture_windows(
    windows: dict, home: Path | None = None, source: str = SOURCE_STATUSLINE
) -> bool:
    """Persist a snapshot built from {window_name: {...}} dicts."""
    snapshot: dict = {
        "captured_at": datetime.now(tz=timezone.utc).isoformat(),
        "source": source,
    }
    captured_any = False
    for window in WINDOW_KEYS:
        window_data = windows.get(window)
        if not isinstance(window_data, dict):
            continue
        entry = _normalize_window(window_data)
        if entry:
            snapshot[window] = entry
            captured_any = True
    if not captured_any:
        return False
    return _write_snapshot(snapshot, home)


def refresh_from_api(
    home: Path | None = None, timeout: float = 15.0
) -> tuple[bool, str | None]:
    """Refresh rate limits by calling the account-session API directly.

    Follows the same channel the claude.ai web/desktop apps use: the
    sessionKey cookie value as a Bearer token against the organizations
    endpoints. Requires the claude.ai session key from PLANTRACK_CLAUDE_SESSION_KEY,
    ~/.local/ptk/session-key, PLANTRACK_SESSION_KEY_FILE, or a session_key_file
    entry in the plantrack config.

    Returns (success, note).
    """
    home = home or paths.default_home()
    session_key = load_session_key(home)
    if not session_key:
        return False, "no claude.ai session key configured"

    org_uuid, note = _fetch_org_uuid(session_key, home, timeout)
    if not org_uuid:
        return False, note or "could not resolve organization uuid"

    # `usage` carries the five_hour/seven_day windows; `rate_limits` answers 200
    # with concurrency tiers only, so it is tried second as a shape fallback.
    for segment in ("usage", "rate_limits"):
        url = f"{API_BASE}/organizations/{org_uuid}/{segment}"
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {session_key}"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return False, f"network error: {exc}"
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        payload_dump.dump(f"claude-org-{segment}", payload)
        windows = _find_windows(payload)
        if windows:
            # The write is the whole point of the refresh: reporting success
            # on a failed write leaves `status` showing stale windows with
            # nothing to explain why.
            if capture_windows(windows, home, source=SOURCE_ACCOUNT_SESSION):
                return True, None
            return False, "could not write the rate limits cache"
    return False, "account session rejected or usage shape unknown"


def _fetch_org_uuid(
    session_key: str, home: Path, timeout: float
) -> tuple[str | None, str | None]:
    """Resolve the organization uuid, live if possible and from cache if not.

    The OAuth token is the credential this endpoint is known to accept, and
    the Claude Code one expires within hours of Claude Code last running -
    precisely the state this refresh is meant to work in. So a stored profile
    is consulted when the live lookups fail: the uuid identifies the account
    and never changes, which makes a cached copy as good as a fetched one.
    """
    tried: list[str] = []
    error: str | None = None
    for token, source in _profile_tokens(session_key, home):
        tried.append(source)
        try:
            response = requests.get(
                PROFILE_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            # Keep going: the remaining credential, and the cached profile
            # below, are exactly what this refresh falls back on when the
            # network is unreliable.
            error = f"network error: {exc}"
            continue
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        uuid = _org_uuid(payload)
        if uuid:
            return uuid, None
    uuid = _org_uuid(_cached_profile(home))
    if uuid:
        return uuid, None
    attempts = ", ".join(tried) or "no usable credential"
    detail = f" ({error})" if error else ""
    return None, f"profile lookup failed via {attempts}{detail}, and no cached profile"


def _org_uuid(payload: object) -> str | None:
    """Pull the organization uuid out of a profile payload."""
    org = payload.get("organization") if isinstance(payload, dict) else None
    uuid = org.get("uuid") if isinstance(org, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


def _cached_profile(home: Path) -> dict | None:
    """Load the stored account profile at any age.

    Imported lazily: claude_profile reads this module for the profile URL and
    session key, so a module-level import would be circular.
    """
    from . import claude_profile

    return claude_profile.load_cached(home, max_age=None)


def _profile_tokens(session_key: str, home: Path) -> list[tuple[str, str]]:
    """Bearer tokens to try against the profile endpoint, in order.

    The session key goes first because it is the durable credential: this
    refresh runs with Claude Code closed, which is exactly when the OAuth
    token has lapsed. Whether the endpoint accepts a session key at all is
    unconfirmed - if it does not, the only cost is one rejected request
    before the OAuth token, then the cached profile, is tried. Note that
    `claude_profile._auth_tokens` orders the same two the other way, for a
    caller that runs while Claude Code is live.
    """
    tokens: list[tuple[str, str]] = [(session_key, "session key")]
    credentials = read_json_dict(paths.claude_dir(home) / ".credentials.json")
    oauth = credentials.get("claudeAiOauth") if credentials else None
    if isinstance(oauth, dict):
        access = oauth.get("accessToken")
        expires = oauth.get("expiresAt")
        if isinstance(access, str) and access:
            if not isinstance(expires, (int, float)) or expires > now_ms():
                tokens.append((access, "claude code oauth"))
    return tokens


def _find_windows(payload: object) -> dict:
    """Find the five_hour/seven_day window dicts in an API payload.

    Known containers are checked first - the payload root, then `data` - so an
    account-level window always outranks a same-named object nested deeper,
    such as a per-model breakdown or an overage section. Only then does the
    breadth-first walk take over, which keeps the refresh working if the
    /usage shape moves again (it has moved once already) without letting an
    unrelated `five_hour` silently become the account's headline number.
    """
    found: dict = {}
    for container in _window_containers(payload):
        for window in WINDOW_KEYS:
            if window in found:
                continue
            candidate = container.get(window)
            if isinstance(candidate, dict) and _normalize_window(candidate):
                found[window] = candidate
        if len(found) == len(WINDOW_KEYS):
            break
    return found


def _window_containers(payload: object) -> Iterator[dict]:
    """Yield dicts that may hold the windows: known roots first, then every
    nested dict breadth-first, so shallower candidates always win.

    A top-level list is walked rather than rejected: the previous lookup
    accepted one, and narrowing that silently would be a regression if the
    endpoint ever answers an array.
    """
    if isinstance(payload, dict):
        yield payload
        nested = payload.get("data")
        if isinstance(nested, dict):
            yield nested
    elif not isinstance(payload, list):
        return
    queue: list[object] = [payload]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            yield current
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)


def _normalize_window(window_data: dict) -> dict | None:
    """Normalize a window dict to {used_percentage, resets_at}, tolerating
    alternate field spellings.

    A window without a usable percentage is rejected outright, even when it
    carries a reset time: such an entry renders as '-' and would still count
    as a capture, letting an API refresh overwrite good statusline data with
    windows that say nothing. `utilization` is the spelling the account
    /organizations/{uuid}/usage endpoint uses.
    """
    entry: dict = {}
    for field in ("used_percentage", "used_percent", "usage_percentage", "utilization"):
        used = window_data.get(field)
        if isinstance(used, (int, float)) and 0 <= used <= 100:
            entry["used_percentage"] = float(used)
            break
    for field in ("resets_at", "reset_at", "resets_at_epoch"):
        resets = window_data.get(field)
        if isinstance(resets, (int, float)) and resets > 1_000_000_000:
            seconds = resets / 1000 if resets > 1e11 else resets
            entry["resets_at"] = int(seconds)
            break
        if isinstance(resets, str) and resets[:4].isdigit():
            parsed = parse_iso(resets)
            if parsed:
                entry["resets_at"] = int(parsed.timestamp())
                break
    return entry if "used_percentage" in entry else None


def _write_snapshot(snapshot: dict, home: Path | None) -> bool:
    target = cache_file(home)
    fileutil.secure_dir(target.parent)
    return fileutil.secure_write_text(target, json.dumps(snapshot))


def load_cached(home: Path | None = None) -> dict | None:
    """Load a fresh (non-stale) rate limits snapshot, if one exists."""
    target = cache_file(home)
    try:
        snapshot = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None
    captured_at = parse_iso(snapshot.get("captured_at"))
    if captured_at is None:
        return None
    if datetime.now(tz=timezone.utc) - captured_at > MAX_AGE:
        return None
    return snapshot


def captured_source(home: Path | None = None) -> str:
    """Name the channel that wrote the cached snapshot.

    Snapshots written before the field existed came from the statusline, which
    was the only writer at the time.
    """
    snapshot = load_cached(home)
    source = snapshot.get("source") if isinstance(snapshot, dict) else None
    return source if isinstance(source, str) and source else SOURCE_STATUSLINE


def captured_at(home: Path | None = None) -> datetime | None:
    """Return when the cached snapshot was written, even if it is stale.

    Callers render the age from this themselves, so a status row served from
    the store still reports how old the underlying capture is *now* rather
    than how old it was when the row was written.
    """
    target = cache_file(home)
    try:
        snapshot = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None
    return parse_iso(snapshot.get("captured_at"))


def captured_age(home: Path | None = None) -> str | None:
    """Return a human-readable age of the last capture, e.g. '3m' or '2h5m'.

    Works even when the cache is stale, so callers can explain why quota
    windows are missing.
    """
    captured = captured_at(home)
    if captured is None:
        return None
    return age_text(datetime.now(tz=timezone.utc) - captured)
