#!/bin/sh
# plantrack statusline wrapper for Claude Code.
#
# Claude Code writes a JSON blob to this script's stdin and renders whatever the
# script prints to stdout as the status line. That blob carries the account's
# 5h/7d rate-limit windows, which is the primary way plantrack learns about
# Claude quota usage.
#
# This wrapper reads the blob once and feeds the same bytes to two consumers:
#
#   1. `ptk capture-claude`, which caches the windows in
#      ~/.local/ptk/claude-rate-limits.json (best effort - see below);
#   2. the user's real statusline command, whose stdout becomes the status line.
#
# Install:
#   cp scripts/statusline-wrapper.sh ~/.claude/statusline-wrapper.sh
#   chmod +x ~/.claude/statusline-wrapper.sh
#
# Wire it into the statusLine entry of ~/.claude/settings.json, passing your
# existing statusline command as the argument:
#
#   "statusLine": {
#     "type": "command",
#     "command": "~/.claude/statusline-wrapper.sh ~/.claude/statusline-command.sh"
#   }
#
# The wrapped command may also be given as PLANTRACK_STATUSLINE_COMMAND (a shell
# string, so it may carry its own arguments). With neither set, the wrapper only
# captures, and prints an empty status line.
#
# Environment:
#   PLANTRACK_STATUSLINE_COMMAND  shell string to run instead of "$@"
#   PLANTRACK_PTK                 plantrack executable (default: ptk)
#   PLANTRACK_CAPTURE_TIMEOUT     seconds allowed for the capture (default: 5)
#
# The capture is strictly best effort: a missing `ptk`, a non-zero exit, a hang,
# or anything it writes to stdout/stderr must never reach or break the status
# line - so it runs under a timeout with both streams sent to /dev/null and its
# exit status discarded.

set -u

ptk_bin=${PLANTRACK_PTK:-ptk}
capture_timeout=${PLANTRACK_CAPTURE_TIMEOUT:-5}

# Read stdin once; the same payload is replayed to both consumers below.
payload=$(cat)

# 1. Best-effort capture. Never allowed to fail the script or emit output.
if [ -n "$payload" ] && command -v "$ptk_bin" >/dev/null 2>&1; then
    if command -v timeout >/dev/null 2>&1; then
        printf '%s\n' "$payload" |
            timeout "$capture_timeout" "$ptk_bin" capture-claude >/dev/null 2>&1 || true
    else
        printf '%s\n' "$payload" | "$ptk_bin" capture-claude >/dev/null 2>&1 || true
    fi
fi

# 2. The user's own statusline command, fed the untouched payload. Its stdout is
# the status line, and its exit status becomes this script's exit status.
if [ "$#" -gt 0 ]; then
    printf '%s\n' "$payload" | "$@"
elif [ -n "${PLANTRACK_STATUSLINE_COMMAND:-}" ]; then
    printf '%s\n' "$payload" | sh -c "$PLANTRACK_STATUSLINE_COMMAND"
fi
