#!/usr/bin/env bash
# Scans for things that must never land in this public repo: private addresses,
# tailnet names, email addresses, MAC addresses, private keys and home paths.
#
# Usage:
#   scripts/check-public.sh           # staged changes (what the pre-commit hook runs)
#   scripts/check-public.sh --all     # every tracked and untracked file
#
# Install as a pre-commit hook:
#   ln -sf ../../scripts/check-public.sh .git/hooks/pre-commit
#
# If ollama is installed, the staged diff is also shown to a local model for a
# second opinion (advisory only). Set SKIP_LLM=1 to skip it, LLM_MODEL to change it.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

SELF="scripts/check-public.sh"
MODE="${1:-staged}"

# Documentation addresses (RFC 5737), example domains and GitHub noreply are fine.
PATTERNS=(
  '(^|[^0-9.])192\.168\.[0-9]{1,3}\.[0-9]{1,3}'
  '(^|[^0-9.])10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'
  '(^|[^0-9.])172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}'
  '(^|[^0-9.])100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}'
  '[A-Za-z0-9-]+\.ts\.net'
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
  '([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}'
  'BEGIN [A-Z ]*PRIVATE KEY'
  '/home/[a-z_][a-z0-9_-]*'
  '\.home\.arpa'
  'tskey-[A-Za-z0-9-]+'
)
ALLOW='example\.ts\.net|@example\.(com|org)|users\.noreply\.github\.com|noreply@anthropic\.com|\$argon2id\$'

if [ "$MODE" = "--all" ]; then
  content=$(git ls-files -co --exclude-standard | grep -vx "$SELF" | while read -r f; do
    [ -f "$f" ] && grep -IHn '' "$f" || true
  done)
else
  # Added lines of the staged diff, prefixed with their file name.
  content=$(git diff --cached -U0 --no-color -- . ":(exclude)$SELF" | awk '
    /^\+\+\+ b\// { file = substr($0, 7); next }
    /^\+/ && !/^\+\+\+/ { print file ": " substr($0, 2) }')
fi

found=0
for p in "${PATTERNS[@]}"; do
  hits=$(printf '%s\n' "$content" | grep -E -- "$p" | grep -Eiv -- "$ALLOW" || true)
  if [ -n "$hits" ]; then
    echo "check-public: possible leak (pattern: $p)" >&2
    printf '%s\n' "$hits" | head -20 | sed 's/^/  /' >&2
    found=1
  fi
done

if [ "$found" = 1 ]; then
  echo "check-public: fix the lines above (use 192.0.2.x / example.ts.net placeholders)." >&2
  exit 1
fi

if [ "$MODE" != "--all" ] && [ -z "${SKIP_LLM:-}" ] && command -v ollama >/dev/null 2>&1; then
  diff=$(git diff --cached --no-color | head -c 60000 || true)
  if [ -n "$diff" ]; then
    echo "check-public: asking local model for a second look..." >&2
    verdict=$(printf 'You review diffs for a PUBLIC git repository. Report any line that leaks personal or local information: real names, email addresses, private IP addresses, hostnames, Tailscale names, usernames, home directory paths, MAC addresses, secrets, keys or passwords. Placeholders such as 192.0.2.x, example.ts.net and the GitHub noreply address are fine. Answer "CLEAN" if nothing leaks, otherwise list the lines.\n\n%s\n' "$diff" \
      | timeout 300 ollama run "${LLM_MODEL:-qwen3-coder:30b-gpu}" 2>/dev/null || echo "(model unavailable)")
    printf '%s\n' "$verdict" | sed -e 's/\x1b\[[0-9;?]*[A-Za-z]//g' -e '/^[[:space:]]*$/d' -e 's/^/  model: /' >&2
  fi
fi
echo "check-public: ok" >&2
