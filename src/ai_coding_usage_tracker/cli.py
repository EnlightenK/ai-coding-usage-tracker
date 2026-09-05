"""Typer CLI entry points for the AI coding plan tracker."""

from __future__ import annotations

import json as json_lib
import os
import sys
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, config, paths, store
from .discovery import (
    MINIMAX_DEFAULT_HOSTS,
    PLAN_LABELS,
    discover_plans,
    sanitize_minimax_host,
)
from .models import PlanStatus, UsageRecord, has_usable_quota
from .parsing import age_text
from .providers import claude, claude_limits, claude_profile, codex
from .scan import collect_scan
from .tracker import DEFAULT_CACHE_TTL, collect_statuses
from .usage import COUNTER_FIELDS, collect_usage

app = typer.Typer(
    help="Track quotas, usage and subscription state for AI coding plans.",
    no_args_is_help=True,
    add_completion=False,
)
plan_app = typer.Typer(help="Manage which coding plans are tracked.", no_args_is_help=True)
app.add_typer(plan_app, name="plan")
history_app = typer.Typer(
    help="Read recorded usage and status history from the local database.",
    no_args_is_help=True,
)
app.add_typer(history_app, name="history")
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the plantrack version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Track quotas, usage and subscription state for AI coding plans."""
    # Runs before every command: one-time move of pre-0.2.0 data into
    # ~/.local/ptk/ (a no-op once the layout is current).
    paths.migrate_legacy(paths.default_home())


def _fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _fmt_used(remaining: float | None) -> str:
    """Format a quota window as used percentage (remaining -> used, inverted)."""
    if remaining is None:
        return "-"
    used = 100.0 - remaining
    text = f"{used:.0f}% used"
    if used >= 90:
        return f"[red]{text}[/red]"
    if used >= 70:
        return f"[yellow]{text}[/yellow]"
    return f"[green]{text}[/green]"


DEFAULT_TZ = "Asia/Hong_Kong"
TZ_ENV = "PLANTRACK_TZ"
CACHE_TTL_ENV = "PLANTRACK_CACHE_TTL"


def _cache_ttl() -> float:
    """Status cache lifetime in seconds (PLANTRACK_CACHE_TTL overrides)."""
    raw = os.environ.get(CACHE_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL
    try:
        return max(0.0, float(raw))
    except ValueError:
        err_console.print(
            f"[yellow]Invalid {CACHE_TTL_ENV}='{escape(raw)}'; "
            f"using {DEFAULT_CACHE_TTL:.0f}s.[/yellow]"
        )
        return DEFAULT_CACHE_TTL


def _resolve_tz(name: str | None) -> timezone | ZoneInfo:
    """Resolve a timezone name, falling back to the system local timezone."""
    candidate = name or DEFAULT_TZ
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if name:
            err_console.print(
                f"[yellow]Unknown timezone '{escape(name)}'; "
                "falling back to system local time.[/yellow]"
            )
    fallback = datetime.now().astimezone().tzinfo
    return fallback if fallback is not None else timezone.utc


def _tz_label(tz: timezone | ZoneInfo) -> str:
    """Return a short display label for a timezone, e.g. 'HKT (UTC+8)'."""
    offset = datetime.now(tz).utcoffset() or timedelta(0)
    hours, minutes = int(offset.total_seconds() // 3600), int(offset.total_seconds() % 3600 // 60)
    sign = "+" if hours >= 0 else "-"
    abbr = datetime.now(tz).strftime("%Z")
    return f"{abbr} (UTC{sign}{abs(hours):02d}:{minutes:02d})"


def _fmt_datetime(value: datetime | None, tz: timezone | ZoneInfo) -> str:
    if value is None:
        return "-"
    local = value.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M")


AUTH_WARNING_DAYS = 7


def _fmt_subscription(status: PlanStatus) -> str:
    sub = status.subscription
    if sub is None:
        if not status.configured:
            return "-"
        return "active (local)"
    parts: list[str] = []
    if sub.plan_type:
        parts.append(escape(sub.plan_type))
    if sub.days_left is not None:
        days = f"{sub.days_left:.0f}d"
        if sub.days_left < 0:
            parts.append(f"[red]expired {days} ago[/red]")
        elif sub.days_left < 7:
            parts.append(f"[yellow]{days} left[/yellow]")
        else:
            parts.append(f"{days} left")
    # Only statuses that mean the plan may stop working: an app-store account
    # keeps a permanently 'incomplete' record that says nothing about health.
    if claude_profile.status_is_concerning(sub.status, sub.billing):
        parts.append(f"[red]{escape(sub.status)}[/red]")
    if sub.auth_days_left is not None and sub.auth_days_left < AUTH_WARNING_DAYS:
        parts.append(f"[yellow](auth {sub.auth_days_left:.0f}d)[/yellow]")
    return " ".join(parts) if parts else "-"


def _fmt_note(status: PlanStatus) -> str:
    """Render the Note cell, ageing the rate-limit capture at display time.

    The age is never stored: a row served from the status cache would
    otherwise keep repeating how old the capture was when the row was
    written, which reads as fresher than the truth.
    """
    parts: list[str] = []
    captured = status.quotas_captured_at
    if captured is not None:
        age = age_text(datetime.now(tz=timezone.utc) - captured)
        if has_usable_quota(status.quotas):
            source = status.quotas_source or claude_limits.SOURCE_STATUSLINE
            parts.append(f"rate limits as of {age} ago ({source})")
        else:
            parts.append(f"stale capture ({age} ago); open a Claude Code session to refresh")
    if status.note:
        parts.append(status.note)
    return escape("; ".join(parts))


def _fmt_time_left(resets_at: datetime, tz: timezone | ZoneInfo) -> str:
    """Format the time until a window resets, e.g. '2h5m left' or '5d3h left'."""
    seconds = (resets_at - datetime.now(tz)).total_seconds()
    if seconds <= 0:
        return "resetting"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m left"
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{rem}m left"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h left"


def _quota_text(status: PlanStatus, kind: str, tz: timezone | ZoneInfo) -> str:
    for quota in status.quotas:
        if quota.kind == kind:
            parts = [_fmt_used(quota.remaining_percent)]
            if quota.resets_at:
                reset = _fmt_datetime(quota.resets_at, tz)
                parts.append(f"{_fmt_time_left(quota.resets_at, tz)} (reset {reset})")
            return ", ".join(parts)
    return "-"


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
    tz_name: str | None = typer.Option(
        None,
        "--tz",
        help=f"Timezone for reset times (default: {DEFAULT_TZ} or {TZ_ENV} env var).",
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Bypass the status cache and fetch live data."
    ),
    home: Path | None = typer.Option(None, help="Home directory to scan."),
) -> None:
    """Show quota windows and subscription state for every discovered plan."""
    tz = _resolve_tz(tz_name or os.environ.get(TZ_ENV))
    statuses = collect_statuses(
        home or paths.default_home(), max_cache_age=_cache_ttl(), refresh=refresh
    )
    if json_output:
        console.print_json(json_lib.dumps([asdict(s) for s in statuses], default=str))
        return
    table = Table(title=f"AI Coding Plans (reset times in {_tz_label(tz)})", show_lines=False)
    table.add_column("Plan", style="bold")
    table.add_column("Auth")
    table.add_column("Active")
    table.add_column("Subscription")
    table.add_column("5h used")
    table.add_column("Weekly used")
    table.add_column("Note", overflow="fold")
    for item in statuses:
        if item.active is True:
            active = "[green]yes[/green]"
        elif item.active is False:
            active = "[red]no[/red]"
        else:
            active = "-"
        table.add_row(
            item.name,
            item.auth_kind,
            active,
            _fmt_subscription(item),
            _quota_text(item, "5h", tz),
            _quota_text(item, "weekly", tz),
            _fmt_note(item),
        )
    console.print(table)


def _usage_table(records: list[UsageRecord], title: str) -> Table:
    """Render daily usage rows plus a totals section."""
    table = Table(title=title)
    table.add_column("Date", style="dim")
    table.add_column("Plan", style="bold")
    table.add_column("Source")
    table.add_column("Model", overflow="fold")
    table.add_column("In", justify="right")
    table.add_column("Out", justify="right")
    table.add_column("Reason", justify="right")
    table.add_column("Cache R", justify="right")
    table.add_column("Cache W", justify="right")
    table.add_column("Reqs", justify="right")
    totals = UsageRecord(date=date.min, source="", plan_id="", model="")
    for record in records:
        table.add_row(
            record.date.isoformat() if record.date != date.min else "unknown",
            PLAN_LABELS.get(record.plan_id, record.plan_id),
            escape(record.source),
            escape(record.model),
            _fmt_tokens(record.input_tokens),
            _fmt_tokens(record.output_tokens),
            _fmt_tokens(record.reasoning_tokens),
            _fmt_tokens(record.cache_read_tokens),
            _fmt_tokens(record.cache_write_tokens),
            str(record.requests),
        )
        totals = replace(
            totals,
            **{field: getattr(totals, field) + getattr(record, field) for field in COUNTER_FIELDS},
        )
    table.add_section()
    table.add_row(
        "",
        "[bold]TOTAL[/bold]",
        "",
        "",
        f"[bold]{_fmt_tokens(totals.input_tokens)}[/bold]",
        f"[bold]{_fmt_tokens(totals.output_tokens)}[/bold]",
        f"[bold]{_fmt_tokens(totals.reasoning_tokens)}[/bold]",
        f"[bold]{_fmt_tokens(totals.cache_read_tokens)}[/bold]",
        f"[bold]{_fmt_tokens(totals.cache_write_tokens)}[/bold]",
        f"[bold]{totals.requests}[/bold]",
    )
    return table


@app.command()
def usage(
    days: int = typer.Option(14, "--days", "-d", min=1, help="Number of days to show."),
    plan: str | None = typer.Option(
        None, "--plan", "-p", help="Filter by plan id (e.g. minimax-cn, glm-intl)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
    home: Path | None = typer.Option(None, help="Home directory to scan."),
) -> None:
    """Show daily token usage from configured providers and local tool logs."""
    if plan is not None:
        _require_plan_id(plan)
    target_home = home or paths.default_home()
    records = collect_usage(target_home, days=days)
    store.record_usage(target_home, records)
    if plan is not None:
        records = [r for r in records if r.plan_id == plan]
    if json_output:
        console.print_json(json_lib.dumps([asdict(r) for r in records], default=str))
        return
    if not records:
        console.print("[dim]No usage found.[/dim]")
        return
    console.print(_usage_table(records, f"Coding plan usage (last {days} day(s))"))


def _require_plan_id(plan_id: str) -> None:
    """Exit with code 2 unless plan_id names a plan plantrack knows about."""
    if plan_id not in PLAN_LABELS:
        valid = ", ".join(PLAN_LABELS)
        err_console.print(f"[red]Unknown plan '{escape(plan_id)}'. Valid: {valid}[/red]")
        raise typer.Exit(code=2)


@plan_app.command("list")
def plan_list(
    home: Path | None = typer.Option(None, help="Home directory to scan."),
) -> None:
    """Show every known plan, its tracking state and where its key comes from."""
    target_home = home or paths.default_home()
    discovered = {p.plan_id: p for p in discover_plans(target_home)}
    disabled = config.disabled_plans(target_home)
    stored_keys = config.manual_keys(target_home)
    table = Table(title="Coding plans known to plantrack")
    table.add_column("Plan", style="bold")
    table.add_column("Plan id")
    table.add_column("State")
    table.add_column("Key sources", overflow="fold")
    for plan_id in PLAN_LABELS:
        sources: list[str] = []
        if plan_id in stored_keys:
            # Discovery already reports a stored key as "plantrack config", but
            # it skips disabled plans entirely: without this the key a disabled
            # plan still holds would go unmentioned.
            sources.append("plantrack config")
        plan = discovered.get(plan_id)
        if plan:
            sources.extend(plan.key_sources)
        # Deduplicate while keeping discovery's order: an enabled plan with a
        # stored key otherwise lists "plantrack config" twice.
        sources = list(dict.fromkeys(sources))
        if plan_id in disabled:
            state = "[red]disabled[/red]"
        elif plan or plan_id in stored_keys:
            state = "[green]tracked[/green]"
        else:
            state = "[dim]not configured[/dim]"
        # Key sources carry user-controlled text (an MCP server name out of
        # ~/.codex/config.toml), so they must not be read as rich markup.
        table.add_row(PLAN_LABELS[plan_id], plan_id, state, escape("; ".join(sources)) or "-")
    console.print(table)


@plan_app.command("add")
def plan_add(
    plan_id: str = typer.Argument(..., help="Plan id to track (see `plantrack plan list`)."),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="API key for the plan's quota API (hidden prompt if omitted).",
    ),
    api_host: str | None = typer.Option(
        None, "--api-host", help="Quota API host override (MiniMax plans only)."
    ),
    home: Path | None = typer.Option(None, help="Home directory for the config file."),
) -> None:
    """Track a plan by storing an API key, even if no tool config exists."""
    _require_plan_id(plan_id)
    if api_host is not None:
        if plan_id not in MINIMAX_DEFAULT_HOSTS:
            err_console.print(
                "[red]--api-host is only supported for MiniMax plans (minimax-cn, "
                "minimax-intl); the GLM quota endpoint is fixed.[/red]"
            )
            raise typer.Exit(code=2)
        # Refuse any host the quota API key could be exfiltrated to; aliases
        # are normalized to their canonical MiniMax host before storing.
        sanitized_host = sanitize_minimax_host(api_host, "")
        if not sanitized_host:
            err_console.print(
                f"[red]Untrusted --api-host '{escape(api_host)}'. Only https URLs on "
                "minimaxi.com or minimax.io are allowed (e.g. "
                "https://www.minimaxi.com, https://www.minimax.io).[/red]"
            )
            raise typer.Exit(code=2)
        api_host = sanitized_host
    if api_key is None:
        # Prompt instead of taking the secret on the command line, where it
        # would leak through argv and shell history.
        api_key = typer.prompt("API key", hide_input=True)
    if not api_key.strip():
        err_console.print("[red]The API key must not be empty.[/red]")
        raise typer.Exit(code=2)
    target_home = home or paths.default_home()
    config_path = escape(str(config.config_file(target_home)))
    if not config.set_manual_key(target_home, plan_id, api_key.strip(), api_host):
        err_console.print(f"[red]Could not write {config_path}.[/red]")
        raise typer.Exit(code=1)
    config.set_disabled(target_home, plan_id, False)
    console.print(f"[green]{PLAN_LABELS[plan_id]} is now tracked via the stored API key.[/green]")
    console.print(f"Key saved to {config_path}; protect this file like a password.")


@plan_app.command("remove")
def plan_remove(
    plan_id: str = typer.Argument(..., help="Plan id to stop tracking."),
    home: Path | None = typer.Option(None, help="Home directory for the config file."),
) -> None:
    """Stop tracking a plan: drops any stored key and disables it."""
    _require_plan_id(plan_id)
    target_home = home or paths.default_home()
    had_key = plan_id in config.manual_keys(target_home)
    config.clear_manual_key(target_home, plan_id)
    config.set_disabled(target_home, plan_id, True)
    if had_key:
        console.print("Removed the API key stored by `plantrack plan add`.")
    console.print(
        f"[green]{PLAN_LABELS[plan_id]} removed from tracking.[/green] "
        f"Restore it later with: plantrack plan enable {plan_id}"
    )


@plan_app.command("disable")
def plan_disable(
    plan_id: str = typer.Argument(..., help="Plan id to hide from status and usage."),
    home: Path | None = typer.Option(None, help="Home directory for the config file."),
) -> None:
    """Hide a plan from status and usage without touching any stored key."""
    _require_plan_id(plan_id)
    config.set_disabled(home or paths.default_home(), plan_id, True)
    console.print(
        f"[green]{PLAN_LABELS[plan_id]} disabled.[/green] "
        f"Re-enable later with: plantrack plan enable {plan_id}"
    )


@plan_app.command("enable")
def plan_enable(
    plan_id: str = typer.Argument(..., help="Plan id to track again."),
    home: Path | None = typer.Option(None, help="Home directory for the config file."),
) -> None:
    """Track a disabled plan again."""
    _require_plan_id(plan_id)
    config.set_disabled(home or paths.default_home(), plan_id, False)
    console.print(f"[green]{PLAN_LABELS[plan_id]} enabled.[/green]")


@app.command()
def scan(
    home: Path | None = typer.Option(None, help="Home directory to scan."),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
) -> None:
    """Scan this PC for coding tool configs, credentials and usage logs."""
    target_home = home or paths.default_home()
    files, logs, plans = collect_scan(target_home)
    if json_output:
        payload = {
            "files": [asdict(entry) for entry in files],
            "logs": [asdict(entry) for entry in logs],
            "plans": [
                {
                    "plan_id": p.plan_id,
                    "name": p.name,
                    "auth_kind": p.auth_kind,
                    "key_sources": p.key_sources,
                }
                for p in plans
            ],
        }
        console.print_json(json_lib.dumps(payload, default=str))
        return
    tz = _resolve_tz(os.environ.get(TZ_ENV))
    file_table = Table(title="Configuration and credential files")
    file_table.add_column("What", style="bold")
    file_table.add_column("Found")
    file_table.add_column("Size", justify="right")
    file_table.add_column("Last modified")
    file_table.add_column("Path", overflow="fold")
    for entry in files:
        file_table.add_row(
            entry.label,
            "[green]yes[/green]" if entry.found else "[dim]no[/dim]",
            _fmt_tokens(entry.size) if entry.size is not None else "-",
            _fmt_datetime(entry.modified, tz) if entry.modified is not None else "-",
            escape(entry.path),
        )
    console.print(file_table)
    log_table = Table(title="Local usage logs")
    log_table.add_column("What", style="bold")
    log_table.add_column("Found")
    log_table.add_column("Files", justify="right")
    log_table.add_column("Path", overflow="fold")
    for entry in logs:
        log_table.add_row(
            entry.label,
            "[green]yes[/green]" if entry.found else "[dim]no[/dim]",
            str(entry.count) if entry.count is not None else "-",
            escape(entry.path),
        )
    console.print(log_table)
    plan_table = Table(title="Discovered plans")
    plan_table.add_column("Plan", style="bold")
    plan_table.add_column("Auth")
    plan_table.add_column("Key sources", overflow="fold")
    for plan in plans:
        # Same user-controlled text as `plan list`: escape before rich sees it.
        plan_table.add_row(plan.name, plan.auth_kind, escape("; ".join(plan.key_sources)))
    if not plans:
        console.print("[dim]No coding plans discovered on this machine.[/dim]")
    else:
        console.print(plan_table)


def _fmt_sub_short(plan_type: str | None, days_left: float | None) -> str:
    """Compact subscription cell for history rows (stored text, never markup)."""
    parts: list[str] = []
    if plan_type:
        parts.append(escape(plan_type))
    if days_left is not None:
        days = f"{days_left:.0f}d"
        if days_left < 0:
            parts.append(f"expired {days} ago")
        else:
            parts.append(f"{days} left")
    return " ".join(parts) or "-"


@history_app.command("usage")
def history_usage(
    days: int = typer.Option(30, "--days", "-d", min=1, help="Number of days to show."),
    plan: str | None = typer.Option(
        None, "--plan", "-p", help="Filter by plan id (e.g. minimax-cn, glm-intl)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
    home: Path | None = typer.Option(None, help="Home directory to scan."),
) -> None:
    """Show recorded daily token usage from the local SQLite store."""
    if plan is not None:
        _require_plan_id(plan)
    records = store.usage_history(home or paths.default_home(), days=days, plan_id=plan)
    if json_output:
        console.print_json(json_lib.dumps([asdict(r) for r in records], default=str))
        return
    if not records:
        console.print("[dim]No recorded usage yet; it is recorded by every `usage` run.[/dim]")
        return
    console.print(_usage_table(records, f"Recorded usage (last {days} day(s))"))


@history_app.command("status")
def history_status(
    hours: int = typer.Option(24, "--hours", min=1, help="How many hours of snapshots to show."),
    plan: str | None = typer.Option(None, "--plan", "-p", help="Filter by plan id."),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
    home: Path | None = typer.Option(None, help="Home directory to scan."),
) -> None:
    """Show recorded status snapshots (subscription and quota windows) over time."""
    if plan is not None:
        _require_plan_id(plan)
    rows = store.status_history(home or paths.default_home(), hours=hours, plan_id=plan)
    if json_output:
        row_keys = ("captured_at", "plan_id", "active", "plan_type", "days_left", "note")
        payload = [
            {**{key: row[key] for key in row_keys}, "quotas": [asdict(q) for q in row["quotas"]]}
            for row in rows
        ]
        console.print_json(json_lib.dumps(payload, default=str))
        return
    tz = _resolve_tz(os.environ.get(TZ_ENV))
    table = Table(title=f"Recorded status (last {hours}h)")
    table.add_column("Time", style="dim")
    table.add_column("Plan", style="bold")
    table.add_column("Active")
    table.add_column("Subscription")
    table.add_column("5h used")
    table.add_column("Weekly used")
    table.add_column("Note", overflow="fold")
    for row in rows:
        quotas = {q.kind: q for q in row["quotas"]}
        if row["active"] is True:
            active = "[green]yes[/green]"
        elif row["active"] is False:
            active = "[red]no[/red]"
        else:
            active = "-"
        table.add_row(
            _fmt_datetime(row["captured_at"], tz),
            PLAN_LABELS.get(row["plan_id"], row["plan_id"]),
            active,
            _fmt_sub_short(row["plan_type"], row["days_left"]),
            _fmt_used(quotas["5h"].remaining_percent) if "5h" in quotas else "-",
            _fmt_used(quotas["weekly"].remaining_percent) if "weekly" in quotas else "-",
            escape(row["note"]) if row["note"] else "",
        )
    if not rows:
        console.print(
            "[dim]No status snapshots yet; they are recorded by every live status check.[/dim]"
        )
        return
    console.print(table)


@app.command("codex-login")
def codex_login(
    timeout: int = typer.Option(
        600, "--timeout", min=1, help="Seconds to wait for browser device login (default: 600)."
    ),
    home: Path | None = typer.Option(None, help="Home directory for Codex credentials."),
) -> None:
    """Authenticate Codex on a headless machine using a ChatGPT device code."""

    def show_code(login: codex.DeviceLogin) -> None:
        console.print("Open this URL in a browser, then enter the device code:")
        console.print(escape(login.verification_url))
        console.print(f"Device code: [bold]{escape(login.user_code)}[/bold]")
        console.print("Waiting for approval…")

    try:
        codex.login_with_device_code(
            show_code, home=home or paths.default_home(), timeout=float(timeout)
        )
    except codex.CodexAppServerTimeout:
        err_console.print("[red]Codex login timed out before approval.[/red]")
        raise typer.Exit(code=1) from None
    except codex.CodexAppServerError as exc:
        err_console.print(f"[red]Codex login failed: {escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print("[green]Codex is authenticated with your ChatGPT plan.[/green]")


def _report_limits_without_session_key(home: Path) -> None:
    """Report the rate-limit state when no claude.ai session key is configured.

    The statusline capture already supplies the 5h/7d windows with no cookie at
    all, so a missing session key is only a failure when that capture has
    produced nothing usable. The local Claude Code OAuth token is not an
    alternative credential here: the organization rate_limits/usage endpoints
    reject it with `account_session_invalid` regardless of its scopes.
    """
    quotas = {q.kind: q for q in claude.cached_quotas(home)}
    age = claude_limits.captured_age(home)
    if quotas:
        source = escape(claude_limits.captured_source(home))
        captured = f" [dim](captured {escape(age)} ago)[/dim]" if age else ""
        console.print(f"[green]Rate limits are current from the {source}.[/green]{captured}")
        for kind, label in (("5h", "5h window"), ("weekly", "Weekly window")):
            if kind in quotas:
                console.print(f"  {label}: {_fmt_used(quotas[kind].remaining_percent)}")
        console.print(
            "[dim]A claude.ai session key is optional. It only adds a refresh that\n"
            "works while Claude Code is closed; see `refresh-claude --help`.[/dim]"
        )
        return
    stale = f" (last statusline capture {age} ago, past the 6h freshness limit)" if age else ""
    err_console.print(f"[yellow]No usable Claude rate limit windows{stale}.[/yellow]")
    err_console.print(
        "Rate limits normally arrive with no credential at all, from the Claude Code\n"
        "statusline capture — run a Claude Code session with the statusline wrapper\n"
        "installed (see the README) and they refresh on their own.\n"
        "\n"
        "To refresh them without Claude Code open, add a claude.ai session key:\n"
        "1. Open claude.ai in your browser, F12 -> Application -> Cookies -> sessionKeyV3\n"
        "2. Copy the value into ~/.local/ptk/session-key (single line)\n"
        "   (alternatives: PLANTRACK_CLAUDE_SESSION_KEY, PLANTRACK_SESSION_KEY_FILE,\n"
        "    or a session_key_file entry in ~/.config/plantrack/config.json)"
    )
    raise typer.Exit(code=2)


@app.command()
def refresh_claude(
    home: Path | None = typer.Option(None, help="Home directory for config/cache."),
) -> None:
    """Refresh Claude subscription state and rate limits from Anthropic.

    The subscription profile only needs the local Claude Code OAuth token.
    Rate limits normally need no credential either — the statusline capture
    keeps them current while Claude Code runs. Refreshing them here instead
    requires the sessionKeyV3 cookie from a logged-in claude.ai session (in
    ~/.local/ptk/session-key, PLANTRACK_CLAUDE_SESSION_KEY,
    PLANTRACK_SESSION_KEY_FILE, or a session_key_file config entry), because
    the organization usage endpoints accept only an account session; the
    OAuth token is rejected there as `account_session_invalid`.
    """
    target_home = home or paths.default_home()
    # Independent of the session key, so it runs before any early exit below.
    profile, profile_note = claude_profile.fetch_profile(target_home)
    if profile is not None:
        info = claude_profile.parse_profile(profile)
        detail = f"[green]Subscription: {escape(info.plan_type or 'unknown')}[/green]"
        if info.billing:
            detail += f" [dim](billing: {escape(info.billing)})[/dim]"
        console.print(detail)
    else:
        err_console.print(
            f"[yellow]Subscription profile unavailable: {escape(profile_note or '')}[/yellow]"
        )
    success, note = claude_limits.refresh_from_api(target_home)
    if success:
        console.print("[green]Refreshed Claude rate limits from claude.ai session.[/green]")
        quotas = {q.kind: q for q in claude.cached_quotas(target_home)}
        for kind, label in (("5h", "5h window"), ("weekly", "Weekly window")):
            if kind in quotas:
                quota = quotas[kind]
                console.print(f"  {label}: {_fmt_used(quota.remaining_percent)}")
    elif note and "no claude.ai session key" in note:
        _report_limits_without_session_key(target_home)
    else:
        err_console.print(f"[red]Refresh failed: {escape(note or '')}[/red]")
        raise typer.Exit(code=1)


@app.command(hidden=True)
def capture_claude(
    home: Path | None = typer.Option(None, help="Home directory for the cache file."),
) -> None:
    """Capture Claude rate limits from statusline JSON piped on stdin."""
    try:
        payload = json_lib.loads(sys.stdin.read())
    except ValueError:
        raise typer.Exit(code=1) from None
    if not isinstance(payload, dict):
        raise typer.Exit(code=1)
    captured = claude_limits.capture_from_statusline_json(payload, home or paths.default_home())
    raise typer.Exit(code=0 if captured else 1)


if __name__ == "__main__":
    app()
