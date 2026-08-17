# Review: commit `8514a32` — 0.2.0 data-home consolidation

**Virtual PR** (no open PR on the repo; reviewed as diff `b6f6c41..8514a32`) · reviewed by clean-context reviewer agent · 2026-08-16

## Summary

Relocates all plantrack-owned state (SQLite DB, Claude caches, payload dumps, session-key default) into a single data home `~/.local/ptk/`, removes the last `Path(__file__)` repo coupling, adds a best-effort one-time migration running before every CLI command, and adds `ptk --version` single-sourced from package metadata. The path consolidation is complete and clean — every writer routes through `paths.ptk_data_dir()` with no stragglers. The issues below are stale user-facing text and test coverage of the new wiring.

## Validation

| Check | Result |
|---|---|
| `uv run pytest -q` | 182 passed |
| `uv run ruff check .` | clean |

## Issues

### Critical

None.

### High

**1. `refresh-claude` still instructs users to write the session key to the removed location** — `src/ai_coding_usage_tracker/cli.py:636-642` (docstring shown in `--help`), `cli.py:663-667` (error-path instructions), `providers/claude_limits.py:104-105` (docstring): all still say "this project's .session-key file". On a machine upgrading from 0.1.0, the key still sits in the old repo `.session-key` (the migration deliberately cannot find it), `refresh-claude` fails with exit 2, and the only in-tool instructions point to a path the code no longer reads — a failure loop. Fix: name `~/.local/ptk/session-key` plus the env/config alternatives in all three places, matching README lines 238-246.

### Medium

**2. `PLANTRACK_SESSION_KEY_FILE` is not `~`-expanded** — `providers/claude_limits.py:34-36` returns `Path(override)` verbatim while line 39 applies `.expanduser()` to the config entry only. Verified: `PLANTRACK_SESSION_KEY_FILE=~/.local/ptk/session-key` silently yields "no claude.ai session key configured" in contexts without shell tilde expansion (systemd, cron, `.env`, Windows). Fix: `Path(override).expanduser()`.

**3. The CLI-level migration wiring has no test** — `tests/test_migration.py` covers `paths.migrate_legacy()` directly, but nothing asserts the `@app.callback()` (`cli.py:53-66`) invokes it; deleting the call line leaves all 182 tests green. Fix: one CliRunner test seeding a legacy layout under a temp `PLANTRACK_HOME`, invoking any command, asserting data lands in `.local/ptk/`.

**4. Two tests run migration against the developer's real `$HOME`** — `tests/test_cli.py:337` and `:233-243` invoke commands without `PLANTRACK_HOME` set, so the callback migrates `C:/Users/Kwong` during pytest (verified empirically). Benign in content but breaks hermeticity and makes tests machine-state-dependent. Fix: autouse conftest fixture pinning `PLANTRACK_HOME` to a tmp dir.

### Low

5. `--home`-passed homes never get migrated (`cli.py:66` always migrates `default_home()`).
6. Never-overwrite is check-then-act, not atomic (`paths.py:66-83`); worst case is two concurrent first-runs racing — acceptable for documented best-effort.
7. Version fallback literal in `__init__.py:10` must be hand-synced with pyproject (inherent `importlib.metadata` tradeoff; add a "keep in sync" comment).
8. Migration `mkdir` uses plain `mkdir(parents=True)` rather than `fileutil.secure_dir` (`paths.py:70,80`) — dir gets umask perms until the first store write; contents keep 0600, so defense-in-depth only.
9. Test-strength nits: env-vs-file precedence test would pass if inverted; no test pins env-file beating config entry; version test checks consistency, not the value.

**Verified sound** (no action): `__init__`/`cli` import ordering, `--version` vs `no_args_is_help`, root callback firing for sub-app commands, no missed legacy consumers, per-command migration cost, no WAL sidecar concerns, migration being CLI-only (by design — the package's public surface is the CLI).

## What's done well

- Complete, atomic path consolidation — all four writers funneled through one `ptk_data_dir()`, zero leftover references outside the intentional migration table.
- Migration design: never-overwrite, OSError-swallowed, no dir creation on clean homes, effectively free after first run — with `test_migration.py` pinning the right invariants.
- Security posture improved: the live claude.ai cookie defaults out of the repo checkout into `~/.local/ptk/session-key` (0600), and the perm tests moved with it.
- `--version` is the idiomatic Typer pattern, exits before any home access; the import-order hazard is documented at the site.
- README data-layout table matches the code file-for-file.

## Recommendation

**Request changes** — the relocation is solid, but High #1 sends upgrading users (the migration's exact audience) into a dead-end failure loop, and Medium #3 means the core wiring can silently regress. Both fixes are small.
