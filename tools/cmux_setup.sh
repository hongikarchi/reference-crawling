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
# Each new workspace's first agent message is a self-discovery prompt
# that anchors it to AGENTS.md (codex auto-loads) + its team file
# (.claude/agents/team-<team>.md). Without this, Codex sessions would
# start blank and act inconsistently across reboots / first-runs.
#
# Idempotent: re-running adds missing workspaces only; existing ones are
# left alone (won't kill running agents or re-send init prompts).

set -euo pipefail

CMUX=/Applications/cmux.app/Contents/Resources/bin/cmux
CWD="/Users/kms_laptop/Documents/archi-tinder/make_db"

# team : start_command  (DB-MAIN excluded — it's the current session)
TEAMS=("DB-CRAWLER:codex" "DB-MATCHER:codex" "DB-ENRICHER:codex" "DB-REVIEWER:claude")

# Self-discovery prompt sent on first start. Same template for all teams;
# only the team name varies. Tells the agent to read its baseline + role
# spec before acting on any subsequent dispatch.
init_prompt() {
    local team_lower="$1"          # e.g. "matcher"
    local team_upper
    team_upper=$(echo "$team_lower" | tr '[:lower:]' '[:upper:]')
    cat <<EOF
You are DB-${team_upper}, one of the 5 cmux workspaces in the make_db Phase 15 multi-team architecture. Before doing any work, read these files in order: 1) AGENTS.md (your baseline + hard guardrails) 2) .claude/agents/team-${team_lower}.md (your specific role + owned files) 3) .claude/PROJECT.md (technical reference for schemas + tools) 4) the most recent 10 lines of .claude/Task.md § Handoffs (recent state). After reading, reply with one short sentence confirming you understand your role and your hard guardrails. Then wait for DB-MAIN to dispatch your first real task via tools/dispatch.sh.
EOF
}

existing=$($CMUX list-workspaces 2>/dev/null | awk '{
    for (i=1; i<=NF; i++) if ($i !~ /^\*?$/ && $i !~ /^workspace:/ && $i !~ /^\[/) print $i
}')

for spec in "${TEAMS[@]}"; do
    name="${spec%%:*}"
    cmd="${spec##*:}"
    team_lower=$(echo "${name#DB-}" | tr '[:upper:]' '[:lower:]')

    if echo "$existing" | grep -qx "$name"; then
        printf "[skip ] %-12s — workspace already exists\n" "$name"
        continue
    fi

    printf "[ new ] %-12s — creating with cmd=%s + init prompt\n" "$name" "$cmd"
    ws_ref=$($CMUX new-workspace --name "$name" --cwd "$CWD" --command "$cmd" --focus false 2>&1 \
              | awk '/^OK/ {print $2}')
    if [ -z "$ws_ref" ]; then
        echo "  failed to capture workspace ref — init prompt skipped" >&2
        continue
    fi

    # Wait for the agent (codex/claude) to finish its own startup splash
    # before typing into its prompt. 8s is empirically enough for either.
    sleep 8

    # Find the first surface of the new workspace
    sref=$($CMUX list-pane-surfaces --workspace "$ws_ref" 2>/dev/null \
            | awk '{for (i=1;i<=NF;i++) if ($i ~ /^surface:/) { print $i; exit }}')
    if [ -z "$sref" ]; then
        echo "  no surface in new workspace $ws_ref — init prompt skipped" >&2
        continue
    fi

    # Send the self-discovery prompt + Enter. Newlines are stripped by
    # cmux send (it types literally), so the heredoc collapses to one
    # long line — perfect for an agent prompt.
    msg=$(init_prompt "$team_lower" | tr '\n' ' ')
    $CMUX send --workspace "$ws_ref" --surface "$sref" "$msg" >/dev/null
    $CMUX send-key --workspace "$ws_ref" --surface "$sref" "Enter" >/dev/null
    printf "         ↳ init prompt sent to %s (%s)\n" "$name" "$sref"
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
