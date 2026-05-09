#!/bin/bash
# quota_check.sh — Phase 16 Rule 2: poll codex /status across team tabs,
# parse weekly + 5h limit %, alert if any tab is at risk.
#
# Usage:  ./tools/quota_check.sh                   # check all tabs
#         ./tools/quota_check.sh enricher          # check one tab
#         ALERT_THRESHOLD=20 ./tools/quota_check.sh # custom threshold
#
# Exit codes:
#   0  — all tabs above ALERT_THRESHOLD weekly
#   1  — at least one tab at or below threshold
#   2  — error reading from a tab (cmux issue)
#
# Logic:
#   For each codex tab, dispatch /status, sleep 5s, read-screen tail 30,
#   regex out "Weekly limit: [....] N% left". Print all + summary.

set -euo pipefail

CMUX=/Applications/cmux.app/Contents/Resources/bin/cmux
ALERT_THRESHOLD="${ALERT_THRESHOLD:-50}"

if [ "$#" -gt 0 ]; then
    TABS=("$1")
else
    TABS=(crawler matcher enricher)
fi

worst_pct=100
worst_tab=""
exit_code=0

for tab in "${TABS[@]}"; do
    name="DB-$(echo "$tab" | tr '[:lower:]' '[:upper:]')"

    # Find workspace ref
    ws_ref=$(
        $CMUX list-workspaces 2>/dev/null \
        | awk -v want="$name" '
            { ref=""; for (i=1;i<=NF;i++) if ($i ~ /^workspace:/) { ref=$i; break }
              if (ref == "") next
              line=$0; sub(/^[* ]*/,"",line); sub(/^workspace:[0-9]+[ \t]*/,"",line)
              sub(/[ \t]*\[selected\][ \t]*$/,"",line); gsub(/^[ \t]+|[ \t]+$/,"",line)
              if (line == want) print ref
            }' | head -1
    )
    if [ -z "$ws_ref" ]; then
        echo "[quota] $name: workspace not found"
        exit_code=2
        continue
    fi

    surf=$(
        $CMUX list-pane-surfaces --workspace "$ws_ref" 2>/dev/null \
        | awk '{for (i=1;i<=NF;i++) if ($i ~ /^surface:/) { print $i; exit }}'
    )

    # Send /status
    $CMUX send --workspace "$ws_ref" --surface "$surf" "/status" >/dev/null
    $CMUX send-key --workspace "$ws_ref" --surface "$surf" "Enter" >/dev/null
    sleep 5

    # Read screen
    raw=$($CMUX read-screen --workspace "$ws_ref" --lines 60 2>&1 || true)

    # Parse weekly % — line like:
    #   "Weekly limit: [████████░░░░░░] 36% left  (resets ...)"
    weekly=$(echo "$raw" | grep -A1 "Weekly limit" | head -2 | tr '\n' ' ' \
             | grep -oE '[0-9]+%' | head -1 | tr -d '%' || true)
    h5=$(echo "$raw" | grep -A1 "5h limit" | head -2 | tr '\n' ' ' \
         | grep -oE '[0-9]+%' | head -1 | tr -d '%' || true)

    if [ -z "$weekly" ]; then
        echo "[quota] $name: could not parse /status output"
        exit_code=2
        continue
    fi

    status_marker="OK "
    if [ "$weekly" -le "$ALERT_THRESHOLD" ]; then
        status_marker="WARN"
        exit_code=1
        if [ "$weekly" -lt "$worst_pct" ]; then
            worst_pct="$weekly"
            worst_tab="$name"
        fi
    fi

    printf "[quota] %s %-14s weekly=%3d%%  5h=%3d%%\n" "$status_marker" "$name" "$weekly" "${h5:-?}"
done

echo ""
if [ "$exit_code" -eq 0 ]; then
    echo "[quota] all tabs above ${ALERT_THRESHOLD}% weekly — safe to dispatch."
elif [ "$exit_code" -eq 1 ]; then
    echo "[quota] WARN: $worst_tab at ${worst_pct}% weekly (≤ ${ALERT_THRESHOLD}%)."
    echo "[quota] Per AGENTS.md Rule 2: STOP and ask user before dispatching > 5K calls."
else
    echo "[quota] ERROR: could not read /status from at least one tab."
fi

exit "$exit_code"
