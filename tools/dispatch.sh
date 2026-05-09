#!/bin/bash
# dispatch.sh — DB-MAIN tab pushes a message into another team workspace.
#
# Usage:    ./tools/dispatch.sh <team> "<message>"
# Example:  ./tools/dispatch.sh matcher "re-run Stage B with phash gate (cycle 1/5)"
#
# Team ∈ {main, crawler, matcher, enricher, reviewer}.
# Resolves to cmux workspace "DB-<TEAM>" → that workspace's first surface
# → `cmux send` + Enter. Idempotent and side-effect-free at the dispatcher
# level; what happens in the target tab depends on what's running there.

set -euo pipefail

CMUX=/Applications/cmux.app/Contents/Resources/bin/cmux

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <team> <message>" >&2
    echo "  team ∈ {main, crawler, matcher, enricher, reviewer}" >&2
    exit 1
fi

TEAM="$1"; shift
MSG="$*"

# Normalize: matcher → DB-MATCHER
WS_NAME="DB-$(echo "$TEAM" | tr '[:lower:]' '[:upper:]')"

# Resolve workspace ref by exact name match (cmux list-workspaces output:
#   "* workspace:5  DB-MAIN  [selected]")
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
    echo "Run ./tools/cmux_setup.sh first. Current workspaces:" >&2
    $CMUX list-workspaces >&2
    exit 2
fi

# Pick the first surface of that workspace
sref=$(
    $CMUX list-pane-surfaces --workspace "$ws_ref" 2>/dev/null \
        | awk '{for (i=1;i<=NF;i++) if ($i ~ /^surface:/) { print $i; exit }}'
)

if [ -z "$sref" ]; then
    echo "ERROR: workspace $ws_ref has no surfaces" >&2
    exit 3
fi

# A. Auto-/clear before EVERY review dispatch (DB-REVIEWER context bloat
# prevention — accumulates 100K+ tokens across self-heal cycles otherwise).
# Opt-out: DISPATCH_NO_CLEAR=1 ./tools/dispatch.sh reviewer "..."
if [ "$TEAM" = "reviewer" ] && [ "${DISPATCH_NO_CLEAR:-0}" != "1" ]; then
    $CMUX send --workspace "$ws_ref" --surface "$sref" "/clear" >/dev/null
    $CMUX send-key --workspace "$ws_ref" --surface "$sref" "Enter" >/dev/null
    sleep 3
    echo "[dispatch] $WS_NAME pre-clear sent"
fi

# C. Send the message. For short prompts use `cmux send` with newlines stripped
# (cmux send types literally and a newline would submit early). For prompts
# > 1500 chars, use `cmux set-buffer + paste-buffer`, which streams the full
# multi-line content into the surface as if pasted — no truncation, no file-
# pointer indirection that codex might misinterpret as an "execute this plan"
# tool-use task.
LIMIT=1500
if [ ${#MSG} -gt "$LIMIT" ]; then
    BUF_NAME="dispatch_${TEAM}_$(date +%Y%m%d_%H%M%S)_$$"
    $CMUX set-buffer --name "$BUF_NAME" "$MSG"
    $CMUX paste-buffer --name "$BUF_NAME" --workspace "$ws_ref" --surface "$sref"
    echo "[dispatch] $WS_NAME pasted ${#MSG} chars via buffer $BUF_NAME"
    # Bash substring expansion (no pipe — avoids SIGPIPE from head -c
    # propagating up via pipefail under subprocess.run capture mode)
    first_line=${MSG%%$'\n'*}
    preview=${first_line:0:80}
else
    flat_msg=$(printf '%s' "$MSG" | tr '\n' ' ')
    $CMUX send --workspace "$ws_ref" --surface "$sref" "$flat_msg"
    preview=${flat_msg:0:80}
fi
# Send Enter twice (Claude Code paste-mode: first Enter closes paste, second
# submits). Safe for Codex (second Enter on empty prompt is a no-op there).
$CMUX send-key --workspace "$ws_ref" --surface "$sref" "Enter"
sleep 0.4
$CMUX send-key --workspace "$ws_ref" --surface "$sref" "Enter"
echo "→ $WS_NAME ($ws_ref / $sref): $(printf '%.80s' "$preview")$([ ${#MSG} -gt 80 ] && echo '…')"
