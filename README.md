# AI Coding Usage Tracker (`plantrack`)

A CLI that tracks quotas, usage and subscription state for your AI coding plan
subscriptions — MiniMax (CN / International), GLM Coding Plan (Z.ai), Claude Code
and ChatGPT Codex — in one table.

```
$ plantrack status
                                                       AI Coding Plans (reset times in HKT (UTC+08:00))
+--------------------------------------------------------------------------------------------------------------------+
| Plan                        | Auth    | Active | Subscription   | 5h used                      | Weekly used                  |
|-----------------------------+---------+--------+----------------+------------------------------+------------------------------|
| MiniMax Coding Plan (CN)    | api-key | yes    | active (local) | 8% used (reset 2026-08-16 20:00) | 22% used (reset 2026-08-17 00:00) |
| MiniMax Coding Plan (Intl)  | api-key | no     | active (local) | -                            | -                            |
| GLM Coding Plan (Z.ai Intl) | api-key | yes    | pro            | 55% used (reset 2026-08-16 17:04) | 28% used (reset 2026-08-17 01:09) |
| Claude Code (Anthropic)     | oauth   | yes    | pro            | 39% used (reset 2026-08-16 16:50) | 33% used (reset 2026-08-16 20:00) |
| ChatGPT Codex (OpenAI)      | oauth   | yes    | plus 25d left  | -                            | -                            |
+--------------------------------------------------------------------------------------------------------------------+
```

Windows are shown as **used** percentages (the same convention as the claude.ai
usage bar, the MiniMax console and the Z.ai console).

## Install

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/). Tracking a
ChatGPT Codex plan from a VPS also requires the
[Codex CLI](https://developers.openai.com/codex/app-server). It is started as a
local stdio child process only; this project never enables its experimental
WebSocket listener.

```bash
uv sync
```

All commands run through the project venv:

```bash
uv run plantrack --help
```

A short alias is installed alongside the full name — `ptk` (3 chars), same
command:

```bash
uv run ptk status
uv run ptk usage -d 7
```

### Install as a global command (no `uv run`)

```bash
uv tool install .
```

After this, `ptk status` (and `plantrack status`) work from any directory and
any shell — Git Bash, PowerShell, CMD — with no `uv run` prefix, and the
installed command is fully independent of this checkout. For development,
`uv tool install --editable .` keeps the command in sync with the working
copy instead. If the command is not found after installing, run
`uv tool update-shell` and restart the shell; updates need
`uv tool install --force .` (or `uv tool upgrade ai-coding-usage-tracker`).

`ptk --version` prints the installed version.

### Ubuntu / VPS setup

Install Python 3.13+, `uv`, and the Codex CLI for the dedicated Unix user that
will run the monitor. For example, after installing Node.js:

```bash
npm install -g @openai/codex
git clone <your-repository-url> ai-coding-usage-tracker
cd ai-coding-usage-tracker
uv sync
uv run plantrack codex-login
```

`codex-login` prints a verification URL and a device code. Open the URL from
your normal browser, approve it, and keep the VPS terminal open until it says
the login completed. The Codex CLI refreshes and stores its own login under
`~/.codex`; do not copy an `auth.json` file from another machine.

Protect the service user's Codex credentials:

```bash
chmod 700 ~/.codex
chmod 600 ~/.codex/auth.json
```

For a custom CLI location (or a test double), set
`PLANTRACK_CODEX_EXECUTABLE=/absolute/path/to/codex`. On shared machines,
prefer an absolute path: the app-server child is resolved via `PATH`
otherwise, and it only ever receives a minimal allowlisted environment
(no secrets you export, such as `PLANTRACK_CLAUDE_SESSION_KEY`).

## Sample commands

### `status` — quota windows and subscription state

```bash
uv run plantrack status                     # all plans, reset times in Hong Kong time (default)
uv run plantrack status --tz Asia/Tokyo     # reset times in another timezone
uv run plantrack status --refresh           # bypass the cache, fetch live data
uv run plantrack status --json              # machine-readable JSON
```

- Quota windows (5-hour / weekly) are fetched live from the provider APIs where
  available (MiniMax CN, GLM, and ChatGPT Codex through the local Codex App
  Server). Other Codex window lengths are retained in JSON output safely.
- Plan checks run in parallel, so one slow provider does not delay the others.
- Results are cached in the local SQLite database for 5 minutes (override with
  `PLANTRACK_CACHE_TTL` seconds, or bypass once with `--refresh`), so repeated
  runs are instant and providers are not hammered. Cached rows say so in the
  Note column.
- Claude Code windows come from the statusline capture (see below).
- Reset times render in `Asia/Hong_Kong` by default. Override per-run with
  `--tz`, or persistently with the `PLANTRACK_TZ` environment variable.

### `usage` — daily token usage

```bash
uv run plantrack usage                      # last 14 days
uv run plantrack usage --days 7             # last 7 days
uv run plantrack usage --days 30 --json     # last 30 days as JSON
uv run plantrack usage --plan glm-intl      # one plan only
uv run plantrack usage --plan chatgpt-codex --days 7
```

Plan ids: `minimax-cn`, `minimax-intl`, `glm-intl`, `claude-code`, `chatgpt-codex`.

For an authenticated ChatGPT Codex plan, usage first comes from account-wide
daily buckets returned by the local Codex App Server. Those buckets are a
total, so `plantrack` records them as input tokens with source `codex-account`
and model `chatgpt-codex account total`; they are preferred over local Codex
session logs to avoid double counting. Request counts are zero for these rows
because the account API does not provide them. If that call is unavailable,
Codex session logs are used as a fallback. The other sources are parsed from
local tool logs:

| Source | Log location |
|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/sessions/**/*.jsonl` + `archived_sessions/` |
| OpenCode | `~/.local/share/opencode/storage/{message,part}/` |

Model names are attributed to plans (`glm-5.2` → GLM plan, `MiniMax-M3` →
MiniMax plan), so usage routed through Claude Code with a third-party endpoint
still lands on the right subscription.

### `plan` — choose which plans are tracked

```bash
uv run plantrack plan list                        # every known plan and its state
uv run plantrack plan disable minimax-intl        # hide a plan you do not use
uv run plantrack plan enable minimax-intl         # track it again
uv run plantrack plan remove minimax-intl         # drop any stored key AND disable
uv run plantrack plan add glm-intl --api-key KEY  # track a plan auto-discovery cannot find
```

Disabled plans disappear from both `status` and `usage`. `plan remove` also
deletes a key stored by `plan add` (use `plan disable` to keep it). Manually
added keys live in `~/.config/plantrack/config.json` — it is created with
owner-only permissions (`0600`, directory `0700`); protect backups the same
way. When `--api-key` is omitted, the command prompts for the key with hidden
input instead of putting it on the command line. `--api-host` (MiniMax plans
only) is validated against the official MiniMax domains and rejects anything
else, so a pasted "mirror" URL cannot receive your key.

### `scan` — inspect what plantrack can see on this PC

```bash
uv run plantrack scan          # tables: config files, usage logs, discovered plans
uv run plantrack scan --json   # the same as JSON
```

Reports which tool configuration and credential files exist (with size and last
modification), how many local usage log files each tool has written, and which
plans discovery found — useful when setting up a new machine or diagnosing a
plan that does not show up.

### `history` — recorded usage and status over time

Every `usage` run records its aggregated daily rows into a local SQLite
database, and every live `status` check stores a timestamped snapshot (kept
for 180 days). History therefore survives local tool logs being rotated or
cleaned up.

```bash
uv run plantrack history usage --days 30              # recorded daily token usage
uv run plantrack history usage --plan glm-intl --json
uv run plantrack history status --hours 24            # status snapshots over time
```

All of plantrack's own records live in one data home, `~/.local/ptk/`
(respecting `PLANTRACK_HOME`):

| File | Purpose |
|---|---|
| `~/.local/ptk/plantrack.db` | SQLite: usage history, status snapshots, status cache |
| `~/.local/ptk/claude-rate-limits.json` | Claude 5h/7d windows from the statusline capture |
| `~/.local/ptk/claude-profile.json` | Claude account profile cache (12h) |
| `~/.local/ptk/session-key` | claude.ai `sessionKeyV3` cookie (see `refresh-claude`) |
| `~/.local/ptk/payloads/` | Raw provider dumps when `PLANTRACK_DEBUG_PAYLOAD=1` |

Configuration (disabled plans, manual keys) stays at
`~/.config/plantrack/config.json`. Nothing points into a repository checkout
unless you explicitly configure it. Data from the pre-0.2.0 layout
(`~/.local/state/plantrack/`, `~/.claude/plantrack-*.json`) is moved into
`~/.local/ptk/` automatically on the first run.

### Debugging provider payloads

Set `PLANTRACK_DEBUG_PAYLOAD=1` to keep the raw JSON of every provider
response under `~/.local/ptk/payloads/` — one file per endpoint,
overwritten on each fetch. Useful to see exactly which fields an endpoint
returns (for example whether a quota API exposes absolute token counts in
addition to percentages):

```bash
PLANTRACK_DEBUG_PAYLOAD=1 uv run plantrack status --refresh
ls ~/.local/ptk/payloads/
```

### `refresh-claude` — refresh Claude subscription state and limits

```bash
uv run plantrack refresh-claude
```

Refreshes two things:

- **Subscription state** from the account profile endpoint. This needs no extra
  setup — the local Claude Code OAuth token is already scoped for it.
- **5h/7d rate limits** from the same Anthropic endpoints the claude.ai web app
  uses. This part is *optional*: the statusline capture below already keeps
  those windows current with no credential at all, and this command reports
  them instead of failing when no session key is configured. Configure a key
  only if you want the numbers refreshed while Claude Code is closed.

The rate-limit endpoints cannot be reached with the local Claude Code OAuth
token — `GET /api/organizations/{uuid}/rate_limits` and `/usage` reject it with
`403 account_session_invalid` whatever its scopes, because they accept only a
claude.ai *account session*. That cookie is what the claude.ai web and desktop
apps send, and it is the only credential that unlocks this path.

The windows live in `/usage`, as `five_hour` and `seven_day` objects keyed on
`utilization` (percent used) plus an ISO `resets_at`. `/rate_limits` answers
200 with per-model concurrency tiers and no usage windows at all, so it is
tried only as a fallback shape. Set `PLANTRACK_DEBUG_PAYLOAD=1` to dump either
response to `~/.local/ptk/payloads/` when the shape changes again.

To configure the key:

1. Open claude.ai in your browser → F12 → Application → Cookies → `sessionKeyV3`
2. Copy the value into `~/.local/ptk/session-key` (single line, no quotes)
3. `chmod 600 ~/.local/ptk/session-key` — it is a live account cookie; treat
   it like a password

The key file never lives in a repository checkout by default. Alternatives:
the `PLANTRACK_CLAUDE_SESSION_KEY` env var, the `PLANTRACK_SESSION_KEY_FILE`
env var (any path), or a persistent `"session_key_file": "/path/to/key"`
entry in `~/.config/plantrack/config.json`.
Once configured, `plantrack status` also auto-refreshes automatically whenever
the statusline capture is stale. If you still have an old `.session-key` in a
project root, move it to `~/.local/ptk/session-key`.

## How each plan is tracked

| Plan | Credentials found in | Quota source |
|---|---|---|
| MiniMax CN | opencode `auth.json`, `~/.claude/settings-mx-cn.json` | Live API (`/v1/token_plan/remains`) |
| MiniMax Intl | `~/.codex/config.toml` (MCP env) | Live API (same endpoint, minimax.io) |
| GLM (Z.ai) | opencode `auth.json`, `~/.claude/settings-glm.json` | Live API (`/api/monitor/usage/quota/limit`) |
| Claude Code | `~/.claude/.credentials.json` (OAuth) | Statusline capture + optional session-key refresh; subscription from `/api/oauth/profile` |
| ChatGPT Codex | `~/.codex/auth.json` (ChatGPT login) | Local Codex App Server (`account/rateLimits/read`, `account/usage/read`) with local session fallback |

### What the Subscription column means

The column shows the plan tier, plus a countdown **only when the provider
reports a real end date** — a ChatGPT Codex plan's `chatgpt_subscription_active_until`,
or a Claude Code trial. An open-ended Claude subscription has no end date to
show, so the cell simply reads `pro` or `max`.

Claude's plan tier, subscription status and billing channel come from
`https://api.anthropic.com/api/oauth/profile`, cached for 12 hours in
`~/.local/ptk/claude-profile.json` (the last known state is reused when
the account is unreachable). The OAuth credential's own lifetime is reported
separately as `auth_days_left` in `--json`, and is appended to the column as
`(auth 3d)` only when re-authentication is due within a week — it is a token
expiry, not a billing date.

A subscription status is only surfaced when it means the plan may stop working
(`past_due`, `canceled`, `unpaid`, …). Accounts billed through the App Store or
Play Store keep a permanently `incomplete` record on Anthropic's side because a
third party collects the payment, so that value is carried in `--json` but
never shown as a warning.

### Claude statusline capture (automatic)

A wrapper at `~/.claude/statusline-wrapper.sh` tees the JSON Claude Code feeds
to the status line into `plantrack capture-claude`, caching the 5h/7d windows
to `~/.local/ptk/claude-rate-limits.json`. Your original
`statusline-command.sh` still renders unchanged. The statusLine entry in
`~/.claude/settings.json` refreshes every 5 minutes, so any open Claude Code
session keeps the numbers current — no manual action needed.

### Scheduled monitoring

Both `status --json` and `usage --json` are suitable for cron, systemd timers,
or a monitoring collector. Run them as the same dedicated user that completed
`codex-login`.

```bash
uv run plantrack status --json > /var/lib/plantrack/status.json
uv run plantrack usage --days 30 --json > /var/lib/plantrack/usage.json
```

For cron, run every 15 minutes (create `/var/lib/plantrack` with ownership for
the service user first). The collected JSON contains account emails, so keep
the directory out of other users' reach:

```bash
install -o plantrack -g plantrack -m 750 -d /var/lib/plantrack
```

```cron
*/15 * * * * cd /opt/ai-coding-usage-tracker && umask 027 && /usr/local/bin/uv run plantrack status --json > /var/lib/plantrack/status.json
```

For systemd, use the same command in a oneshot service and timer:

```ini
# /etc/systemd/system/plantrack-status.service
[Service]
Type=oneshot
User=plantrack
WorkingDirectory=/opt/ai-coding-usage-tracker
ExecStart=/bin/sh -c '/usr/local/bin/uv run plantrack status --json > /var/lib/plantrack/status.json'
```

```ini
# /etc/systemd/system/plantrack-status.timer
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

This observes the authenticated ChatGPT Codex plan wherever you use Codex, but
provider-specific local detail (Claude transcripts, OpenCode storage, and local
Codex fallback sessions) still requires those agents and their configuration to
exist on the monitored machine.

Note that `status --json` includes the account email for plans that report one
(Claude, Codex), so protect the collected files the same way as other
credentials.

## Development

```bash
uv run pytest           # run the test suite
uv run pytest -q        # quiet mode
uv run ruff check .     # lint (config in pyproject.toml)
```

Project layout:

```
src/ai_coding_usage_tracker/
├── cli.py              # Typer commands (status, usage, plan, scan, ...)
├── config.py           # user config: disabled plans and manual API keys
├── discovery.py        # credential auto-discovery from local tool configs
├── models.py           # dataclasses shared across providers
├── parsing.py          # shared JSON/timestamp/token parsing helpers
├── paths.py            # config directory resolution (PLANTRACK_HOME override)
├── scan.py             # local-PC scan of tool configs, credentials and logs
├── store.py            # SQLite: usage history, status snapshots, status cache
├── tracker.py          # assembles PlanStatus for each discovered plan (parallel)
├── usage.py            # aggregation of usage records across providers
└── providers/
    ├── minimax.py      # MiniMax token plan remains API
    ├── zai.py          # Z.ai monitor quota API
    ├── claude.py       # Claude OAuth state + transcript parsing
    ├── claude_limits.py# Claude rate limit cache + session-key refresh
    ├── claude_profile.py # Claude account profile (plan tier, billing, status)
    ├── codex.py        # Codex JWT claims + session log parsing
    └── opencode.py     # OpenCode storage parsing
```

Use `PLANTRACK_HOME=<dir>` to point discovery at a fake home directory
(used by the tests).
