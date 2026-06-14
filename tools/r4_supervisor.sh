#!/bin/bash
# R4 unattended supervisor: drives the text then vision enrichment to 100%
# with automatic codex<->claude failover.
#
# Hard-won rules baked in:
#   - ONE lane at a time. Running codex + claude together thrashes this
#     10-core machine (measured 15/min vs 57/min solo).
#   - codex is preferred (faster, ~57/min @16w). claude (Haiku) only fills in
#     while codex quota is exhausted.
#   - codex quota is probed before each lane and every PROBE_EVERY while claude
#     runs; the moment codex is back we kill claude and hand the work back.
#   - vision also fails over to claude: `claude -p` reads the cover image via
#     its Read tool (verified). codex preferred; claude fills quota gaps.
#   - all runners are resume-safe (sidecar done-cid skip), so kill/restart is free.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

TEXT_WORKERS=16
CLAUDE_WORKERS=4          # batched: each call does CLAUDE_BATCH items
CLAUDE_BATCH=40           # amortizes claude -p's ~18.7k agent overhead ~40x
VISION_WORKERS=16
CLAUDE_VISION_WORKERS=5   # vision claude reads 1 image/call; keep modest
PROBE_EVERY=1200          # 20 min — recheck codex while claude is running
ENRICH="python3 tools/r4_axis_enrich.py"
VISION="python3 tools/r4_vision_enrich.py"

log() { echo "[r4-sup $(date '+%H:%M:%S')] $*"; }

codex_alive() {
  local out
  out=$(codex exec --skip-git-repo-check -c model=gpt-5.5 \
        -c model_reasoning_effort=low -c service_tier=fast \
        'Reply with exactly: OK' 2>&1)
  echo "$out" | grep -qi 'usage limit' && return 1
  echo "$out" | grep -q 'OK' && return 0
  return 1
}

text_pending()   { $ENRICH --count 2>/dev/null | tail -1; }
vision_pending() { $VISION --count 2>/dev/null | tail -1; }

# claude lane works the tail while codex is down; poll codex; hand back the
# moment it returns. $1=command to run, $2=phase label for logging.
run_claude_until_codex_back() {
  local cmd="$1" label="$2"
  log "codex quota exhausted -> claude ${label} lane"
  eval "$cmd" > /tmp/r4_claude_lane.log 2>&1 &
  local cl=$!
  while kill -0 "$cl" 2>/dev/null; do
    sleep "$PROBE_EVERY"
    if codex_alive; then
      log "codex back -> stopping claude, returning to codex"
      kill "$cl" 2>/dev/null; pkill -f 'claude -p' 2>/dev/null
      wait "$cl" 2>/dev/null
      return
    fi
    log "claude ${label} still running, codex still down"
  done
  log "claude ${label} lane exited on its own"
}

# ---- Phase TEXT --------------------------------------------------------------
log "TEXT phase start: $(text_pending) pending"
while [ "$(text_pending)" -gt 0 ]; do
  if codex_alive; then
    log "codex lane (${TEXT_WORKERS}w): $(text_pending) pending"
    $ENRICH --engine codex --workers "$TEXT_WORKERS" > /tmp/r4_codex_lane.log 2>&1
    # exit 3 = abort-streak (quota died mid-run); loop re-probes below
  else
    run_claude_until_codex_back \
      "$ENRICH --engine claude --reverse --workers $CLAUDE_WORKERS --batch $CLAUDE_BATCH" "text"
  fi
done
log "TEXT phase COMPLETE: $(text_pending) pending"

# ---- Phase VISION (codex preferred, claude fallback via Read-tool image) -----
log "VISION phase start: $(vision_pending) pending"
while [ "$(vision_pending)" -gt 0 ]; do
  if codex_alive; then
    log "vision codex lane (${VISION_WORKERS}w): $(vision_pending) pending"
    $VISION --engine codex --workers "$VISION_WORKERS" > /tmp/r4_vision_lane.log 2>&1
  else
    run_claude_until_codex_back \
      "$VISION --engine claude --workers $CLAUDE_VISION_WORKERS" "vision"
  fi
done
log "VISION phase COMPLETE. R4 enrichment done."
