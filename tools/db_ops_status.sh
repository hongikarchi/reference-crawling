#!/bin/bash
# Summarize make_db DB Ops state at session start.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

echo "== make_db DB Ops Status =="
date "+%Y-%m-%d %H:%M:%S %Z"
echo

echo "== Git =="
git status --short --branch
echo

echo "== Modified files =="
git status --short | sed -n '1,80p'
echo

echo "== Latest Handoffs =="
if [ -f .claude/Task.md ]; then
  awk '
    /^## Handoffs/ { in_h=1; next }
    /^## / && in_h { in_h=0 }
    in_h && NF { print }
  ' .claude/Task.md | tail -20
else
  echo "missing .claude/Task.md"
fi
echo

echo "== Report Header =="
if [ -f .claude/REPORT.md ]; then
  sed -n '1,40p' .claude/REPORT.md
else
  echo "missing .claude/REPORT.md"
fi
echo

echo "== Recent Ops Jobs =="
find .claude/ops/jobs -type f -name '*.md' 2>/dev/null | sort | tail -5 || true
echo

echo "== Recent Ops Runs =="
find .claude/ops/runs -type f -name '*.md' 2>/dev/null | sort | tail -5 || true
echo

echo "== Recent Logs =="
if [ -d logs ]; then
  find logs -maxdepth 1 -type f -print0 2>/dev/null \
    | xargs -0 ls -t 2>/dev/null \
    | sed -n '1,8p'
else
  echo "no logs/ directory"
fi
echo

echo "== Running PID Records =="
find /tmp -maxdepth 1 -type f \( -name '*make_db*pid*' -o -name '*d1*pid*' -o -name '*d2*pid*' -o -name '*phash*pid*' -o -name '*stage*pid*' \) -print 2>/dev/null | sort || true
echo

echo "Next: create/read a job card under .claude/ops/jobs/ before non-trivial work."

