#!/bin/bash
# cmux 5-workspace setup for make_db (Phase 15 multi-team architecture).
#
# Each team gets its OWN workspace (sidebar entry), mirroring make_web's
# MAIN/REVIEW/BACK/FRONT layout.
#
#   DB-MAIN      — Claude Code (Opus orchestrator, this session)
#   DB-CRAWLER   — Codex CLI (writes/fixes crawl/<source>/*)
#   DB-MATCHER   — Codex CLI (writes/fixes canonical/match_*.py + phash_cache.py)
#   DB-ENRICHER  — Codex CLI (writes/fixes enrich/* + harness)
#   DB-REVIEWER  — Claude Code (Opus, reviewer_gate.py + LLM spot-checks)
#
# Idempotent: re-running adds missing workspaces only; existing ones are
# left alone (won't kill running agents).

set -euo pipefail

CMUX=/Applications/cmux.app/Contents/Resources/bin/cmux
CWD="/Users/kms_laptop/Documents/archi-tinder/make_db"

# team : start_command  (DB-MAIN excluded — it's the current session)
TEAMS=("DB-CRAWLER:codex" "DB-MATCHER:codex" "DB-ENRICHER:codex" "DB-REVIEWER:claude")

existing=$($CMUX list-workspaces 2>/dev/null | awk '{
    for (i=1; i<=NF; i++) if ($i !~ /^\*?$/ && $i !~ /^workspace:/ && $i !~ /^\[/) print $i
}')

for spec in "${TEAMS[@]}"; do
    name="${spec%%:*}"
    cmd="${spec##*:}"

    if echo "$existing" | grep -qx "$name"; then
        printf "[skip ] %-12s — workspace already exists\n" "$name"
        continue
    fi

    printf "[ new ] %-12s — creating with cmd=%s\n" "$name" "$cmd"
    $CMUX new-workspace --name "$name" --cwd "$CWD" --command "$cmd" --focus false >/dev/null
done

# Ensure DB-MAIN exists with the right name (this script may run from
# anywhere; if the current workspace is unnamed, name it).
if ! echo "$existing" | grep -qx "DB-MAIN"; then
    cur_ws=$($CMUX identify 2>/dev/null \
        | awk -F'"' '/workspace_ref/ {print $4}' | head -1)
    if [ -n "$cur_ws" ]; then
        printf "[name ] DB-MAIN      — renaming current workspace %s\n" "$cur_ws"
        $CMUX workspace-action --action rename --workspace "$cur_ws" --title "DB-MAIN" >/dev/null
    fi
fi

echo ""
echo "✓ DB workspaces ready"
$CMUX list-workspaces | grep -E "DB-(MAIN|CRAWLER|MATCHER|ENRICHER|REVIEWER)" || true
echo ""
echo "  dispatch:  ./tools/dispatch.sh <team> \"<message>\""
echo "  team ∈ {main, crawler, matcher, enricher, reviewer}"
