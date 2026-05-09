# Known bugs in current tooling — fix in Phase 17 Step 1

## Bug #1: `tools/dispatch_enrich_batch.py` `_looks_idle()` hangs

### Symptom

Phase 16 first smoke run (2026-05-09 12:53):
- DB-ENRICHER tab received batch, processed in 1m 48s, returned valid
  JSON list (verified via `./tools/poll.sh enricher`).
- DB-MAIN's `dispatch_enrich_batch.py` script hung 16+ minutes after
  codex finished. Process was alive (`ps -p 60349`) but in `sleep` cycle.
- Eventually script reached usage-limit branch (codex weekly cap
  reduction triggered before timeout) and went into 5797-second sleep.

### Root cause (suspected)

`tools/dispatch_enrich_batch.py:239`:
```python
def _looks_idle(raw: str) -> bool:
    tail = "\n".join(raw.splitlines()[-12:]).lower()
    return "›" in tail or "tokens used" in tail or "token" in tail and "used" in tail
```

Issues:
1. The `›` (codex idle prompt) appears in the tab even DURING processing
   (in tooltip/spinner area), not only after completion. False positive.
2. `tokens used` line appears AFTER the JSON response (footer). But by
   the time read-screen captures the tail-12 lines, codex has already
   moved past tokens-used into a fresh prompt placeholder ("Find and
   fix a bug in @filename" type sample text). Tokens-used scrolls out
   of the 12-line tail.
3. Operator precedence bug in last `or` clause:
   ```
   "›" in tail OR ("tokens used" in tail) OR ("token" in tail AND "used" in tail)
   ```
   The parenthesization actually evaluates as the `OR` chain, not as
   intended (tokens-used always implies token AND used, redundant).

### Fix sketch (Phase 17 Step 1 dispatch to DB-ENRICHER)

```python
def _looks_idle(raw: str) -> bool:
    """Return True iff codex has completed its response and is awaiting
    the next prompt. We detect this with TWO signals that must BOTH be
    present in the most recent activity window:

    1. The codex 'tokens used' footer block is present (codex prints
       'tokens used\n<NUMBER>' after each response).
    2. AFTER that footer, there is a fresh prompt prompt marker (line
       starting with `›` followed by sample-prompt placeholder, OR
       blank prompt line above the footer status bar).

    We DO NOT just check tail-12 lines because codex animations push
    the footer out of frame quickly. Instead, search the whole `raw`
    for the most recent 'tokens used' occurrence and verify it appears
    AFTER our last dispatched user prompt marker."""

    tokens_idx = raw.rfind("tokens used")
    if tokens_idx == -1:
        return False
    # After 'tokens used' there should be at least one numeric line
    # (the count) and then a fresh prompt marker.
    after = raw[tokens_idx:]
    return "›" in after  # the prompt placeholder is the conclusive signal
```

### Verification (after fix)

`tests/test_dispatch_enrich_batch.py::test_poll_screen_reads_mocked_cmux_output_and_extracts_json`
should still pass. ADD a new test:
- `test_idle_detection_handles_long_response_with_footer_after_response`:
  raw = "<long batch JSON>\ntokens used\n45123\n› Find and fix a bug in
  @filename\n" → must return True.
- `test_idle_detection_false_during_processing`:
  raw = "...processing...\ntokens used\n123\n... still working" → must
  return False (no fresh prompt marker after footer).

### Workaround until fixed

If Phase 17 Step 1 fix is delayed, run smoke with very short timeout
(`--timeout-seconds 60`) and explicit batch limit (`--max-batches 1`).
Manual `./tools/poll.sh enricher` to verify completion before next batch.

---

## Bug #2: cmux send 1500-char silent truncation

### Status

FIXED in commit `3059ce8` (2026-05-09): `tools/dispatch.sh` now writes
plans > 1500 chars to `/tmp/dispatch-{team}-{ts}.md` and dispatches a
short pointer instead.

### Verification

```bash
# 1700-char message:
LONG=$(python3 -c "print('a' * 1700)")
./tools/dispatch.sh crawler "$LONG"
# Expected: "[dispatch] DB-CRAWLER plan → /tmp/dispatch-crawler-...md (1705 chars; sending pointer)"
# Expected: tab receives "Long plan: read /tmp/...md and execute"
```

Pass criterion: confirm `/tmp/dispatch-crawler-*.md` was created with
full message body (1700 a's), and the tab's prompt line shows the
short pointer message (not the truncated 'a's).

---

## Bug #3 (potential): Spark model untested

### Status

Open. `/status` shows GPT-5.3-Codex-Spark with separate 100% quotas
(5h + weekly), but we have not verified:
- Quality of Spark for vocab classification + 60-100 word desc
- Whether `codex exec -c model=gpt-5.3-codex-spark` actually works (or
  if it requires a different flag)

### Phase 17 Step 1 dispatch (low priority — fallback if Pro quota tight)

```bash
./tools/dispatch.sh enricher "Run a smoke: codex exec -c model=gpt-5.3-codex-spark
'Output ONLY {test:ok}'. Then re-run with model=gpt-5.5 and compare:
quality, latency, token cost. Report verdict on whether Spark is
suitable for D-1 vocab + visual_description tasks."
```

If suitable, Spark becomes a 100%-quota fallback when Pro quota tight.
