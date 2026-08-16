"""Optional dumping of raw provider payloads, for API research and debugging.

Set PLANTRACK_DEBUG_PAYLOAD=1 to keep the raw JSON of every provider response
the tracker fetches under <home>/.local/ptk/payloads/ — one file per endpoint,
overwritten on each fetch. Payloads are wrapped as {"_fetched_at": ...,
"payload": ...} so the raw content stays intact.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import fileutil, paths

DUMP_ENV = "PLANTRACK_DEBUG_PAYLOAD"
_FALSY = {"", "0", "false", "no"}
_DIRNAME = "payloads"


def enabled() -> bool:
    """Whether raw payload dumping is switched on."""
    return os.environ.get(DUMP_ENV, "").strip().lower() not in _FALSY


def dump_dir(home: Path | None = None) -> Path:
    """Return the directory raw payloads are written to."""
    home = home or paths.default_home()
    return paths.ptk_data_dir(home) / _DIRNAME


def dump(name: str, payload: object, home: Path | None = None) -> bool:
    """Write one endpoint's latest raw payload; False when disabled or unwritable."""
    if not enabled():
        return False
    target = dump_dir(home) / f"{name}.json"
    wrapper = {"_fetched_at": datetime.now(tz=timezone.utc).isoformat(), "payload": payload}
    try:
        content = json.dumps(wrapper, indent=2)
    except (TypeError, ValueError):
        return False
    fileutil.secure_dir(target.parent)
    return fileutil.secure_write_text(target, content)
