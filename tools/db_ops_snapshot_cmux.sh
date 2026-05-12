#!/bin/bash
# Snapshot DB-related cmux workspaces before clearing/restarting them.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

CMUX="${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUTDIR=".claude/ops/snapshots/$STAMP"

if [ ! -x "$CMUX" ]; then
  echo "ERROR: cmux binary not executable: $CMUX" >&2
  exit 2
fi

mkdir -p "$OUTDIR"

"$CMUX" list-workspaces > "$OUTDIR/workspaces.txt" 2>&1 || {
  echo "ERROR: cmux list-workspaces failed" >&2
  exit 3
}

awk '
  {
    ref=""
    for (i=1; i<=NF; i++) if ($i ~ /^workspace:/) { ref=$i; break }
    if (ref == "") next
    line=$0
    sub(/^[* ]*/, "", line)
    sub(/^workspace:[0-9]+[ \t]*/, "", line)
    sub(/[ \t]*\[selected\][ \t]*$/, "", line)
    gsub(/^[ \t]+|[ \t]+$/, "", line)
    if (line ~ /^DB-/) print ref "\t" line
  }
' "$OUTDIR/workspaces.txt" > "$OUTDIR/db-workspaces.tsv"

if [ ! -s "$OUTDIR/db-workspaces.tsv" ]; then
  echo "No DB-* cmux workspaces found. Snapshot inventory saved to $OUTDIR/workspaces.txt"
  exit 0
fi

while IFS=$'\t' read -r ws_ref ws_name; do
  safe_name="$(printf '%s' "$ws_name" | tr -cd 'A-Za-z0-9_.-')"
  surf_file="$OUTDIR/${safe_name}.surfaces.txt"
  "$CMUX" list-pane-surfaces --workspace "$ws_ref" > "$surf_file" 2>&1 || true
  sref="$(awk '{ for (i=1;i<=NF;i++) if ($i ~ /^surface:/) { print $i; exit } }' "$surf_file")"
  if [ -z "$sref" ]; then
    echo "WARN: $ws_name has no surface" >> "$OUTDIR/warnings.txt"
    continue
  fi
  "$CMUX" read-screen --workspace "$ws_ref" --surface "$sref" --lines 500 --scrollback \
    > "$OUTDIR/${safe_name}.txt" 2>&1 || true
done < "$OUTDIR/db-workspaces.tsv"

cat > "$OUTDIR/README.md" <<EOF
# cmux Snapshot $STAMP

Created by \`tools/db_ops_snapshot_cmux.sh\`.

Purpose: preserve terminal-visible state before clear/restart/migration to
DB Ops Mode. This snapshot is read-only evidence, not source of truth.

Files:
- \`workspaces.txt\` — raw cmux workspace list.
- \`db-workspaces.tsv\` — DB-* workspaces captured.
- \`DB-*.txt\` — scrollback snapshots.
EOF

echo "Snapshot saved: $OUTDIR"

