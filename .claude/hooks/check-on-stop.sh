#!/usr/bin/env bash
# Claude Code Stop hook: when files changed, run the tests before Claude finishes.
# Exit 2 sends the failures back to Claude. A second stop in a row is let through
# (stop_hook_active), so Claude can still report a problem it can't fix.
set -uo pipefail
input="$(cat)"
case "$input" in *'"stop_hook_active": true'*|*'"stop_hook_active":true'*) exit 0 ;; esac
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
[ -z "$(git status --porcelain 2>/dev/null)" ] && exit 0
if out="$(./scripts/test.sh 2>&1)"; then exit 0; fi
printf 'Tests are failing. Fix them, or explain why you are stopping:\n%s\n' "$(printf '%s\n' "$out" | tail -40)" >&2
exit 2
