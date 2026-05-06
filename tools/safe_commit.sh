#!/bin/bash
# safe_commit.sh — DB-MAIN's commit fallback when a codex tab gets stuck.
#
# Codex sometimes can't finish a commit (sandbox `git apply --cached` patch
# corruption, auto-reviewer hang, etc.). When that happens, DB-MAIN runs
# this to land the staged work without losing context.
#
# Usage:
#   ./tools/safe_commit.sh "<subject>"
#   ./tools/safe_commit.sh "<subject>" "<body line>" "<body line>" ...
#
# Behavior:
#   1. Refuses to stage files whose paths look like secrets (.env*,
#      credentials, secret, private_key) — those should never accidentally
#      land in a fallback commit.
#   2. Stages all modified + untracked (`git add -A`). For tighter control,
#      stage manually before invocation and pass --staged-only.
#   3. Composes commit message with subject + optional body lines + the
#      Phase 15 multi-team co-author trailers.
#   4. Refuses to push (per Phase 15 git policy).

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <subject> [body line] [body line]..." >&2
    exit 1
fi

STAGED_ONLY=0
if [ "${1:-}" = "--staged-only" ]; then
    STAGED_ONLY=1
    shift
fi

SUBJECT="$1"
shift
BODY_LINES=("$@")

# Refuse to stage suspicious files
DANGER_PATTERNS=("\.env$" "\.env\." "credentials" "private_key" "secret")
SUSPICIOUS=()
while IFS= read -r f; do
    for pat in "${DANGER_PATTERNS[@]}"; do
        if echo "$f" | grep -qE "$pat"; then
            SUSPICIOUS+=("$f")
            break
        fi
    done
done < <(git status --porcelain | awk '$1 ~ /^[?MA]/ {print $2}')

if [ "${#SUSPICIOUS[@]}" -gt 0 ]; then
    echo "ABORT: refusing to commit suspicious files:" >&2
    for f in "${SUSPICIOUS[@]}"; do echo "  $f" >&2; done
    exit 2
fi

# Stage everything (unless --staged-only)
if [ "$STAGED_ONLY" -ne 1 ]; then
    git add -A
fi

if git diff --cached --quiet; then
    echo "no staged changes to commit"
    exit 0
fi

# Compose message
MSG_ARGS=("-m" "$SUBJECT")
for line in "${BODY_LINES[@]}"; do
    MSG_ARGS+=("-m" "$line")
done
MSG_ARGS+=("-m" "Co-Authored-By: Codex CLI (DB-MATCHER) <noreply@openai.com>")
MSG_ARGS+=("-m" "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>")

git commit "${MSG_ARGS[@]}"
git log --oneline -1
