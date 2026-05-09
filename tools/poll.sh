#!/bin/bash
# poll.sh — DB-MAIN reads the latest output of another team's tab.
#
# Usage:    ./tools/poll.sh <team>[:<surface_idx>] [lines]
# Example:  ./tools/poll.sh matcher 100
# Example:  ./tools/poll.sh enricher:27 100
#
# Wraps `cmux read-screen` for the named team's workspace. Useful for:
#  - confirming a dispatched task ran (or errored)
#  - reading Codex's diff/output before deciding next step
#  - parsing the team's <TEAM>-DONE / -ESCALATE signal in real time
#    (instead of relying on .claude/Task.md polling alone)
#
# Default lines = 60. Pass `--scrollback` as the 3rd arg to see history.

set -euo pipefail

CMUX=/Applications/cmux.app/Contents/Resources/bin/cmux

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <team>[:<surface_idx>] [lines] [--scrollback]" >&2
    echo "  team ∈ {main, crawler, matcher, enricher, reviewer}" >&2
    exit 1
fi

TARGET="$1"
LINES="${2:-60}"
SCROLLBACK="${3:-}"
TEAM="${TARGET%%:*}"
SURFACE_IDX=""
if [[ "$TARGET" == *:* ]]; then
    SURFACE_IDX="${TARGET#*:}"
    if [ -z "$TEAM" ] || [ -z "$SURFACE_IDX" ]; then
        echo "ERROR: invalid target '$TARGET' (expected <team>[:<surface_idx>])" >&2
        exit 1
    fi
fi

WS_NAME="DB-$(echo "$TEAM" | tr '[:lower:]' '[:upper:]')"

ws_ref=$(
    $CMUX list-workspaces 2>/dev/null \
        | awk -v want="$WS_NAME" '
            {
                ref=""
                for (i=1; i<=NF; i++) if ($i ~ /^workspace:/) { ref=$i; break }
                if (ref == "") next
                line=$0
                sub(/^[* ]*/, "", line)
                sub(/^workspace:[0-9]+[ \t]*/, "", line)
                sub(/[ \t]*\[selected\][ \t]*$/, "", line)
                gsub(/^[ \t]+|[ \t]+$/, "", line)
                if (line == want) print ref
            }
        ' \
        | head -1
)

if [ -z "$ws_ref" ]; then
    echo "ERROR: no workspace named '$WS_NAME'" >&2
    exit 2
fi

surfaces=$($CMUX list-pane-surfaces --workspace "$ws_ref" 2>/dev/null)
if [ -n "$SURFACE_IDX" ]; then
    want_surface="surface:$SURFACE_IDX"
    sref=$(
        printf '%s\n' "$surfaces" \
            | awk -v want="$want_surface" '
                { for (i=1;i<=NF;i++) if ($i == want) { print $i; exit } }
            '
    )
    if [ -z "$sref" ]; then
        echo "ERROR: workspace $ws_ref ($WS_NAME) has no surface '$want_surface'" >&2
        echo "Current surfaces:" >&2
        printf '%s\n' "$surfaces" >&2
        exit 3
    fi
else
    sref=$(
        printf '%s\n' "$surfaces" \
            | awk '{for (i=1;i<=NF;i++) if ($i ~ /^surface:/) { print $i; exit }}'
    )
fi

if [ -z "$sref" ]; then
    echo "ERROR: workspace $ws_ref has no surfaces" >&2
    exit 3
fi

if [ "$SCROLLBACK" = "--scrollback" ]; then
    $CMUX read-screen --workspace "$ws_ref" --surface "$sref" --lines "$LINES" --scrollback
else
    $CMUX read-screen --workspace "$ws_ref" --surface "$sref" --lines "$LINES"
fi
