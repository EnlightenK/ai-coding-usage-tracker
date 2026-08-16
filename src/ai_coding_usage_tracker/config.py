"""User configuration: tracked-plan switches and manually registered API keys."""

from __future__ import annotations

import json
from pathlib import Path

from . import fileutil, paths

CONFIG_FILENAME = "config.json"


def config_file(home: Path | None = None) -> Path:
    """Return the plantrack config path inside the scanned home."""
    home = home or paths.default_home()
    return home / ".config" / "plantrack" / CONFIG_FILENAME


def load_config(home: Path | None = None) -> dict:
    """Load the config mapping, returning {} for anything unusable."""
    try:
        data = json.loads(config_file(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(home: Path | None, config: dict) -> bool:
    """Persist the config mapping; False when the file cannot be written."""
    target = config_file(home)
    fileutil.secure_dir(target.parent)
    return fileutil.secure_write_text(target, json.dumps(config, indent=2) + "\n")


def disabled_plans(home: Path | None = None) -> set[str]:
    """Plan ids the user has removed from tracking."""
    value = load_config(home).get("disabled")
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def set_disabled(home: Path | None, plan_id: str, disabled: bool) -> bool:
    """Add or remove one plan id from the disabled list."""
    config = load_config(home)
    value = config.get("disabled")
    ids = {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()
    if disabled:
        ids.add(plan_id)
    else:
        ids.discard(plan_id)
    config["disabled"] = sorted(ids)
    return save_config(home, config)


def manual_keys(home: Path | None = None) -> dict[str, dict]:
    """Plan ids to API-key entries supplied via `plantrack plan add`."""
    value = load_config(home).get("api_keys")
    return value if isinstance(value, dict) else {}


def set_manual_key(
    home: Path | None, plan_id: str, api_key: str, api_host: str | None = None
) -> bool:
    """Store an API key (and optional host) for one plan."""
    config = load_config(home)
    keys = config.get("api_keys")
    keys = keys if isinstance(keys, dict) else {}
    entry: dict = {"api_key": api_key}
    if api_host:
        entry["api_host"] = api_host
    keys[plan_id] = entry
    config["api_keys"] = keys
    return save_config(home, config)


def clear_manual_key(home: Path | None, plan_id: str) -> bool:
    """Drop a manually stored API key, if one exists."""
    config = load_config(home)
    keys = config.get("api_keys")
    if isinstance(keys, dict) and plan_id in keys:
        del keys[plan_id]
        config["api_keys"] = keys
    return save_config(home, config)
