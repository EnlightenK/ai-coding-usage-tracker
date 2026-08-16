"""Claude rate limits: statusline capture, account-session refresh, cache."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .. import fileutil, paths, payload_dump
from ..parsing import now_ms, parse_iso, read_json_dict

CACHE_FILENAME = "plantrack-claude-rate-limits.json"
SESSION_KEY_FILENAME = ".session-key"
SESSION_KEY_ENV = "PLANTRACK_CLAUDE_SESSION_KEY"
SESSION_KEY_FILE_ENV = "PLANTRACK_SESSION_KEY_FILE"
MAX_AGE = timedelta(hours=6)
API_BASE = "https://api.anthropic.com/api"
PROFILE_URL = API_BASE + "/oauth/profile"

WINDOW_KEYS = ("five_hour", "seven_day")


def project_root() -> Path:
    """Return this project's root directory."""
    return Path(__file__).resolve().parents[3]


def session_key_file() -> Path:
    """Return the session key file path inside this project."""
    override = os.environ.get(SESSION_KEY_FILE_ENV)
    if override:
        return Path(override)
    return project_root() / SESSION_KEY_FILENAME


def cache_file(home: Path | None = None) -> Path:
    """Return the path of the Claude rate limits cache file."""
    home = home or paths.default_home()
    return paths.claude_dir(home) / CACHE_FILENAME


def load_session_key(home: Path | None = None) -> str | None:
    """Load the claude.ai account session key from env var or project file.

    The key is the `sessionKeyV3` cookie value from a logged-in claude.ai
    browser session. It is stored in PLANTRACK_CLAUDE_SESSION_KEY or in
    the project's .session-key file.
    """
    from_env = os.environ.get(SESSION_KEY_ENV)
    if from_env and from_env.strip():
        return from_env.strip()
    try:
        content = session_key_file().read_text(encoding="utf-8")
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


def capture_windows(windows: dict, home: Path | None = None) -> bool:
    """Persist a snapshot built from {window_name: {...}} dicts."""
    snapshot: dict = {"captured_at": datetime.now(tz=timezone.utc).isoformat()}
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
    the project's .session-key file, or a path via PLANTRACK_SESSION_KEY_FILE.

    Returns (success, note).
    """
    home = home or paths.default_home()
    session_key = load_session_key(home)
    if not session_key:
        return False, "no claude.ai session key configured"

    org_uuid, note = _fetch_org_uuid(session_key, home, timeout)
    if not org_uuid:
        return False, note or "could not resolve organization uuid"

    for segment in ("rate_limits", "usage"):
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
            capture_windows(windows, home)
            return True, None
    return False, "account session rejected or usage shape unknown"


def _fetch_org_uuid(
    session_key: str, home: Path, timeout: float
) -> tuple[str | None, str | None]:
    """Resolve the organization uuid via the oauth profile endpoint.

    Prefers the Claude Code OAuth token (already scoped for profile);
    falls back to the account session key.
    """
    for token, _source in _profile_tokens(session_key, home):
        try:
            response = requests.get(
                PROFILE_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return None, f"network error: {exc}"
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        org = payload.get("organization")
        uuid = org.get("uuid") if isinstance(org, dict) else None
        if isinstance(uuid, str) and uuid:
            return uuid, None
    return None, f"profile lookup failed via {_source}"


def _profile_tokens(session_key: str, home: Path) -> list[tuple[str, str]]:
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
    """Recursively find five_hour/seven_day window dicts in an API payload."""
    found: dict = {}
    for window in WINDOW_KEYS:
        candidate = _find_key(payload, window)
        if isinstance(candidate, dict) and _normalize_window(candidate):
            found[window] = candidate
    return found


def _find_key(payload: object, key: str) -> object | None:
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k == key:
                return v
            nested = _find_key(v, key)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _find_key(item, key)
            if nested is not None:
                return nested
    return None


def _normalize_window(window_data: dict) -> dict | None:
    """Normalize a window dict to {used_percentage, resets_at}, tolerating
    alternate field spellings."""
    entry: dict = {}
    for field in ("used_percentage", "used_percent", "usage_percentage"):
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
    return entry or None


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


def captured_age(home: Path | None = None) -> str | None:
    """Return a human-readable age of the last capture, e.g. '3m' or '2h5m'.

    Works even when the cache is stale, so callers can explain why quota
    windows are missing.
    """
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
    seconds = int((datetime.now(tz=timezone.utc) - captured_at).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes, hours = seconds % 3600 // 60, seconds // 3600
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"
