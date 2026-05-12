#!/bin/bash
# Create the DB Ops cmux layout without deleting legacy DB-* workspaces.

set -euo pipefail

CMUX="${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
CWD="/Users/kms_laptop/Documents/archi-tinder/make_db"
WITH_WORKER=0

if [ "${1:-}" = "--worker" ]; then
  WITH_WORKER=1
fi

if [ ! -x "$CMUX" ]; then
  echo "ERROR: cmux binary not executable: $CMUX" >&2
  exit 2
fi

TEAMS=(
  "DB-CODEX-OPS:codex -C $CWD -c model_reasoning_effort=low"
  "DB-RUNNER:zsh -l"
  "DB-MONITOR:zsh -l"
  "DB-CLAUDE-GATE:claude"
)

if [ "$WITH_WORKER" = "1" ]; then
  TEAMS+=("DB-CODEX-WORKER:codex -C $CWD -c model_reasoning_effort=low")
fi

init_prompt() {
  local name="$1"
  case "$name" in
    DB-CODEX-OPS)
      cat <<'EOF'
You are DB-CODEX-OPS for make_db. Read AGENTS.md, CLAUDE.md, and .claude/DB_OPS.md first. Your role: operational control plane, job cards, smoke ladders, code/validation, run records, compact Claude Gate packets. Do not use legacy DB-MAIN dispatch unless explicitly asked. Reply with one short confirmation, then wait.
EOF
      ;;
    DB-CLAUDE-GATE)
      cat <<'EOF'
You are DB-CLAUDE-GATE for make_db. Read CLAUDE.md, .claude/DB_OPS.md, and .claude/agents/team-reviewer.md first. Your role: semantic/architecture checkpoint review from compact packets under .claude/ops/reviews/. Do not run broad orchestration or full log review. Reply with one short confirmation, then wait.
EOF
      ;;
    DB-CODEX-WORKER)
      cat <<'EOF'
You are DB-CODEX-WORKER for make_db. Read AGENTS.md and .claude/DB_OPS.md first. Only accept bounded side tasks from DB-CODEX-OPS: code search, diff review, or 1-2 file patch with explicit write scope. Reply with one short confirmation, then wait.
EOF
      ;;
    *)
      return 1
      ;;
  esac
}

existing="$("$CMUX" list-workspaces 2>/dev/null || true)"

for spec in "${TEAMS[@]}"; do
  name="${spec%%:*}"
  cmd="${spec#*:}"

  if printf '%s\n' "$existing" | awk -v want="$name" '
    {
      ref=""
      for (i=1; i<=NF; i++) if ($i ~ /^workspace:/) { ref=$i; break }
      if (ref == "") next
      line=$0
      sub(/^[* ]*/, "", line)
      sub(/^workspace:[0-9]+[ \t]*/, "", line)
      sub(/[ \t]*\[selected\][ \t]*$/, "", line)
      gsub(/^[ \t]+|[ \t]+$/, "", line)
      if (line == want) found=1
    }
    END { exit(found ? 0 : 1) }
  '; then
    printf "[skip ] %-16s workspace already exists\n" "$name"
    continue
  fi

  printf "[ new ] %-16s command=%s\n" "$name" "$cmd"
  ws_ref="$("$CMUX" new-workspace --name "$name" --cwd "$CWD" --command "$cmd" --focus false 2>&1 \
    | awk '/^OK/ { print $2; exit }')"
  if [ -z "$ws_ref" ]; then
    echo "WARN: could not capture workspace ref for $name; init skipped" >&2
    continue
  fi

  sleep 8
  sref="$("$CMUX" list-pane-surfaces --workspace "$ws_ref" 2>/dev/null \
    | awk '{ for (i=1;i<=NF;i++) if ($i ~ /^surface:/) { print $i; exit } }')"
  if [ -z "$sref" ]; then
    echo "WARN: no surface for $name; init skipped" >&2
    continue
  fi

  if prompt="$(init_prompt "$name")"; then
    flat="$(printf '%s' "$prompt" | tr '\n' ' ')"
    "$CMUX" send --workspace "$ws_ref" --surface "$sref" "$flat" >/dev/null
    "$CMUX" send-key --workspace "$ws_ref" --surface "$sref" "Enter" >/dev/null
    printf "       init prompt sent to %s (%s)\n" "$name" "$sref"
  fi
done

echo
echo "DB Ops workspaces:"
"$CMUX" list-workspaces | grep -E "DB-(CODEX-OPS|RUNNER|MONITOR|CLAUDE-GATE|CODEX-WORKER)" || true
echo
echo "Tip: run tools/db_ops_snapshot_cmux.sh before clearing legacy DB-* workspaces."

