"""OpenCode provider: local usage parsing keyed by coding-plan provider IDs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

from .. import paths
from ..discovery import OPENCODE_PROVIDER_TO_PLAN
from ..models import UsageRecord
from ..parsing import ms_to_datetime, positive_int, read_json


def iter_usage(home: Path | None = None, since: date | None = None) -> Iterator[UsageRecord]:
    """Yield per-step usage records from OpenCode message and part storage."""
    home = home or paths.default_home()
    storage = paths.opencode_data_dir(home) / "storage"
    message_dir = storage / "message"
    part_dir = storage / "part"
    if not message_dir.is_dir() or not part_dir.is_dir():
        return
    messages = _load_messages(message_dir)
    for file in sorted(part_dir.rglob("*.jsonl")) + sorted(part_dir.rglob("*.json")):
        record = _parse_part(file, messages, since)
        if record is not None:
            yield record


def _load_messages(message_dir: Path) -> dict[str, tuple[str, str, date | None]]:
    messages: dict[str, tuple[str, str, date | None]] = {}
    for file in sorted(message_dir.rglob("*.json")):
        data = read_json(file)
        if not isinstance(data, dict):
            continue
        message_id = data.get("id")
        model = data.get("model")
        if not isinstance(message_id, str) or not isinstance(model, dict):
            continue
        provider_id = model.get("providerID")
        model_id = model.get("modelID")
        if not isinstance(provider_id, str) or not isinstance(model_id, str):
            continue
        created = None
        time = data.get("time")
        if isinstance(time, dict):
            created = _ms_to_date(time.get("created"))
        messages[message_id] = (provider_id, model_id, created)
    return messages


def _parse_part(
    file: Path,
    messages: dict[str, tuple[str, str, date | None]],
    since: date | None,
) -> UsageRecord | None:
    data = read_json(file)
    if not isinstance(data, dict) or data.get("type") != "step-finish":
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    message_id = data.get("messageID")
    info = messages.get(message_id) if isinstance(message_id, str) else None
    provider_id = info[0] if info else "unknown"
    model_id = info[1] if info else "unknown"
    created = info[2] if info else None
    plan_id = OPENCODE_PROVIDER_TO_PLAN.get(provider_id)
    if plan_id is None:
        return None
    day = created or date.min
    if since is not None and day < since:
        return None
    cache = tokens.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    return UsageRecord(
        date=day,
        source="opencode",
        plan_id=plan_id,
        model=model_id,
        input_tokens=positive_int(tokens.get("input")),
        output_tokens=positive_int(tokens.get("output")),
        reasoning_tokens=positive_int(tokens.get("reasoning")),
        cache_read_tokens=positive_int(cache.get("read")),
        cache_write_tokens=positive_int(cache.get("write")),
        requests=1,
    )


def _ms_to_date(value: object) -> date | None:
    stamp = ms_to_datetime(value)
    return stamp.date() if stamp else None
