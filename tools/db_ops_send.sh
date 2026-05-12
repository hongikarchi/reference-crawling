#!/bin/bash
# Send text to a DB Ops cmux workspace.

set -euo pipefail

CMUX="${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <ops|runner|monitor|claude-gate|worker|DB-NAME> <message>" >&2
  exit 1
fi

TARGET="$1"; shift
MSG="$*"

case "$TARGET" in
  ops|codex-ops) WS_NAME="DB-CODEX-OPS" ;;
  runner) WS_NAME="DB-RUNNER" ;;
  monitor) WS_NAME="DB-MONITOR" ;;
  claude|claude-gate|gate) WS_NAME="DB-CLAUDE-GATE" ;;
  worker|codex-worker) WS_NAME="DB-CODEX-WORKER" ;;
  DB-*) WS_NAME="$TARGET" ;;
  *)
    echo "ERROR: unknown DB Ops target '$TARGET'" >&2
    exit 2
    ;;
esac

if [ ! -x "$CMUX" ]; then
  echo "ERROR: cmux binary not executable: $CMUX" >&2
  exit 2
fi

ws_ref="$("$CMUX" list-workspaces 2>/dev/null | awk -v want="$WS_NAME" '
  {
    ref=""
    for (i=1; i<=NF; i++) if ($i ~ /^workspace:/) { ref=$i; break }
    if (ref == "") next
    line=$0
    sub(/^[* ]*/, "", line)
    sub(/^workspace:[0-9]+[ \t]*/, "", line)
    sub(/[ \t]*\[selected\][ \t]*$/, "", line)
    gsub(/^[ \t]+|[ \t]+$/, "", line)
    if (line == want) { print ref; exit }
  }
')"

if [ -z "$ws_ref" ]; then
  echo "ERROR: no workspace named '$WS_NAME'. Run tools/db_ops_cmux_setup.sh first." >&2
  exit 3
fi

sref="$("$CMUX" list-pane-surfaces --workspace "$ws_ref" 2>/dev/null \
  | awk '{ for (i=1;i<=NF;i++) if ($i ~ /^surface:/) { print $i; exit } }')"

if [ -z "$sref" ]; then
  echo "ERROR: workspace '$WS_NAME' has no surface" >&2
  exit 4
fi

LIMIT=1500
if [ ${#MSG} -gt "$LIMIT" ]; then
  BUF_NAME="db_ops_${TARGET}_$(date +%Y%m%d_%H%M%S)_$$"
  "$CMUX" set-buffer --name "$BUF_NAME" "$MSG"
  "$CMUX" paste-buffer --name "$BUF_NAME" --workspace "$ws_ref" --surface "$sref"
  preview="${MSG%%$'\n'*}"
else
  flat_msg="$(printf '%s' "$MSG" | tr '\n' ' ')"
  "$CMUX" send --workspace "$ws_ref" --surface "$sref" "$flat_msg"
  preview="$flat_msg"
fi

"$CMUX" send-key --workspace "$ws_ref" --surface "$sref" "Enter" >/dev/null
sleep 0.4
"$CMUX" send-key --workspace "$ws_ref" --surface "$sref" "Enter" >/dev/null

echo "-> $WS_NAME ($ws_ref / $sref): ${preview:0:100}"

