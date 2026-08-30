"""SQLite store: usage history, status snapshots and the status cache."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fileutil, paths
from .models import PlanStatus, QuotaWindow, UsageRecord
from .parsing import parse_iso
from .providers import codex
from .usage import COUNTER_FIELDS

DB_FILENAME = "plantrack.db"
SNAPSHOT_RETENTION = timedelta(days=180)

# Sources within a group are alternative views of the *same* underlying usage,
# so a given (date, plan) may be represented by at most one of them: recording
# one supersedes whatever a sibling recorded for that day.  Codex, for example,
# reports either account-wide totals or locally parsed sessions per run, and
# keeping both would double-count the day in `usage_history`.
EXCLUSIVE_SOURCE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({codex.SESSION_SOURCE, codex.ACCOUNT_SOURCE}),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_days (
    date TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, plan_id, source, model)
);
CREATE TABLE IF NOT EXISTS status_snapshots (
    captured_at TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    active INTEGER,
    plan_type TEXT,
    days_left REAL,
    quotas TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    PRIMARY KEY (captured_at, plan_id)
);
CREATE TABLE IF NOT EXISTS status_cache (
    plan_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    stored_at TEXT NOT NULL
);
"""


def db_path(home: Path | None = None) -> Path:
    """Return the SQLite database path inside the scanned home."""
    home = home or paths.default_home()
    return paths.ptk_data_dir(home) / DB_FILENAME


# Database paths whose schema this process has already created.  Applying the
# schema on every connection is wasted work on read-only paths (`cached_status`
# runs once per plan), and it is only ever needed once per file.
_SCHEMA_APPLIED: set[Path] = set()


def _connect(home: Path | None) -> sqlite3.Connection:
    target = db_path(home)
    fileutil.secure_dir(target.parent)
    # Checked before connecting, which itself creates an empty file: a database
    # removed behind our back needs its schema again even if we once applied it.
    existed = target.exists()
    conn = sqlite3.connect(target)
    if not existed or target not in _SCHEMA_APPLIED:
        conn.executescript(_SCHEMA)
        _SCHEMA_APPLIED.add(target)
    # The DB file now certainly exists; keep it owner-only where chmod applies.
    fileutil.secure_file(target)
    return conn


# --- usage history -----------------------------------------------------------


def record_usage(home: Path | None, records: list[UsageRecord]) -> int:
    """Upsert aggregated daily usage rows. Returns the row count written.

    Parsing local logs is idempotent, so a later run replaces (not adds to)
    the numbers it already recorded for the same (date, plan, source, model).
    Rows left by an EXCLUSIVE_SOURCE_GROUPS sibling of an incoming source are
    dropped for the days this batch covers, so the newest run's channel wins.
    """
    if not records:
        return 0
    columns = ", ".join(COUNTER_FIELDS)
    placeholders = ", ".join("?" for _ in COUNTER_FIELDS)
    updates = ", ".join(f"{field} = excluded.{field}" for field in COUNTER_FIELDS)
    with closing(_connect(home)) as conn, conn:
        superseded = _superseded_rows(records)
        if superseded:
            conn.executemany(
                "DELETE FROM usage_days WHERE date = ? AND plan_id = ? AND source = ?",
                superseded,
            )
        conn.executemany(
            f"""
            INSERT INTO usage_days (date, plan_id, source, model, {columns})
            VALUES (?, ?, ?, ?, {placeholders})
            ON CONFLICT (date, plan_id, source, model) DO UPDATE SET {updates}
            """,
            [
                (
                    record.date.isoformat(),
                    record.plan_id,
                    record.source,
                    record.model,
                    *(getattr(record, field) for field in COUNTER_FIELDS),
                )
                for record in records
            ],
        )
    return len(records)


def _superseded_rows(records: list[UsageRecord]) -> list[tuple[str, str, str]]:
    """Return (date, plan_id, source) keys this batch makes obsolete.

    Only days, plans and sources the batch actually covers are listed, so
    unrelated plans, sources and dates keep whatever they recorded earlier.
    """
    superseded: set[tuple[str, str, str]] = set()
    for group in EXCLUSIVE_SOURCE_GROUPS:
        covered: dict[tuple[str, str], set[str]] = {}
        for record in records:
            if record.source in group:
                key = (record.date.isoformat(), record.plan_id)
                covered.setdefault(key, set()).add(record.source)
        for (day, plan_id), incoming in covered.items():
            superseded.update((day, plan_id, source) for source in group - incoming)
    return sorted(superseded)


def usage_history(
    home: Path | None, days: int = 30, plan_id: str | None = None
) -> list[UsageRecord]:
    """Return recorded daily usage for the last `days` days, newest first."""
    since = (
        datetime.now(tz=timezone.utc).date() - timedelta(days=days - 1)
    ).isoformat()
    query = (
        f"SELECT date, plan_id, source, model, {', '.join(COUNTER_FIELDS)} "
        "FROM usage_days WHERE date >= ?"
    )
    params: list[str] = [since]
    if plan_id is not None:
        query += " AND plan_id = ?"
        params.append(plan_id)
    query += " ORDER BY date DESC, plan_id, source, model"
    with closing(_connect(home)) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        UsageRecord(
            date=datetime.fromisoformat(row[0]).date(),
            plan_id=row[1],
            source=row[2],
            model=row[3],
            **dict(zip(COUNTER_FIELDS, row[4:], strict=True)),
        )
        for row in rows
    ]


# --- status snapshots and cache ----------------------------------------------


def record_status(home: Path | None, statuses: list[PlanStatus]) -> None:
    """Store one timestamped snapshot per plan and refresh the status cache."""
    now = datetime.now(tz=timezone.utc)
    rows = [
        (
            now.isoformat(),
            status.plan_id,
            None if status.active is None else int(status.active),
            status.subscription.plan_type if status.subscription else None,
            status.subscription.days_left if status.subscription else None,
            json.dumps(
                [
                    {
                        "kind": quota.kind,
                        "remaining_percent": quota.remaining_percent,
                        "resets_at": quota.resets_at.isoformat()
                        if quota.resets_at
                        else None,
                    }
                    for quota in status.quotas
                ]
            ),
            _snapshot_note(status),
        )
        for status in statuses
    ]
    cutoff = (now - SNAPSHOT_RETENTION).isoformat()
    with closing(_connect(home)) as conn, conn:
        conn.executemany(
            """
            INSERT INTO status_snapshots
                (captured_at, plan_id, active, plan_type, days_left, quotas, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (captured_at, plan_id) DO UPDATE SET
                active = excluded.active,
                plan_type = excluded.plan_type,
                days_left = excluded.days_left,
                quotas = excluded.quotas,
                note = excluded.note
            """,
            rows,
        )
        conn.execute("DELETE FROM status_snapshots WHERE captured_at < ?", (cutoff,))
        conn.executemany(
            """
            INSERT INTO status_cache (plan_id, payload, stored_at)
            VALUES (?, ?, ?)
            ON CONFLICT (plan_id) DO UPDATE SET
                payload = excluded.payload,
                stored_at = excluded.stored_at
            """,
            [
                (
                    status.plan_id,
                    json.dumps(_status_payload(status), default=str),
                    now.isoformat(),
                )
                for status in statuses
            ],
        )


def _snapshot_note(status: PlanStatus) -> str | None:
    """Note text for a history row, keeping which channel captured the quotas.

    A history row is read long after it was written, so the *source* of the
    numbers is worth recording; how old the capture was at write time is not,
    and is rendered live from `quotas_captured_at` instead.
    """
    parts = [f"rate limits from {status.quotas_source}"] if status.quotas_source else []
    if status.note:
        parts.append(status.note)
    return "; ".join(parts) or None


def _status_payload(status: PlanStatus) -> dict:
    return asdict(status)


def cached_status(
    home: Path | None, plan_id: str, max_age_seconds: float
) -> tuple[dict, datetime] | None:
    """Return (payload, stored_at) for a fresh cache entry, else None."""
    if max_age_seconds <= 0:
        return None
    with closing(_connect(home)) as conn:
        row = conn.execute(
            "SELECT payload, stored_at FROM status_cache WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    if row is None:
        return None
    stored_at = parse_iso(row[1])
    if stored_at is None:
        return None
    if datetime.now(tz=timezone.utc) - stored_at > timedelta(seconds=max_age_seconds):
        return None
    try:
        payload = json.loads(row[0])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload, stored_at


def status_history(
    home: Path | None, hours: int = 24, plan_id: str | None = None
) -> list[dict]:
    """Return recorded status snapshots from the last `hours`, newest first."""
    since = (datetime.now(tz=timezone.utc) - timedelta(hours=hours)).isoformat()
    query = (
        "SELECT captured_at, plan_id, active, plan_type, days_left, quotas, note "
        "FROM status_snapshots WHERE captured_at >= ?"
    )
    params: list[str] = [since]
    if plan_id is not None:
        query += " AND plan_id = ?"
        params.append(plan_id)
    query += " ORDER BY captured_at DESC, plan_id"
    with closing(_connect(home)) as conn:
        rows = conn.execute(query, params).fetchall()
    history: list[dict] = []
    for row in rows:
        captured_at = parse_iso(row[0])
        if captured_at is None:
            continue
        try:
            quotas_raw = json.loads(row[5])
        except ValueError:
            quotas_raw = []
        quotas = [
            QuotaWindow(
                kind=quota.get("kind") or "",
                remaining_percent=quota.get("remaining_percent"),
                resets_at=parse_iso(quota.get("resets_at")),
            )
            for quota in quotas_raw
            if isinstance(quota, dict)
        ]
        history.append(
            {
                "captured_at": captured_at,
                "plan_id": row[1],
                "active": None if row[2] is None else bool(row[2]),
                "plan_type": row[3],
                "days_left": row[4],
                "quotas": quotas,
                "note": row[6],
            }
        )
    return history
