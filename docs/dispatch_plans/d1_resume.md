# Dispatch plan: D-1 text enrichment resume (5/15+ Phase 17 Step 3)

**Target tab**: DB-ENRICHER
**Stage**: D-1 (text enrich, vocab + visual_description)
**Created**: 2026-05-09 (Phase 16 prep)
**Executes**: 2026-05-15 21:24 KST or later (after weekly quota reset)

---

## Pre-launch checks (Rule 1, 2, 3, 4 from AGENTS.md)

### Rule 2 — `/status` quota check (run BEFORE dispatch)

```bash
./tools/dispatch.sh enricher "/status"
sleep 5
./tools/poll.sh enricher 30
```

Acceptance: weekly limit ≥ 95% (target — fresh after reset).

### Rule 3 — Codex pre-investigation (run BEFORE writing dispatch script)

```bash
./tools/dispatch.sh enricher "What's the most token-efficient pattern in
codex CLI 0.129 for processing a batch of 30 small text-classification
prompts? Compare: (a) one codex exec per cid, (b) one codex exec with all
30 inlined as a JSON array prompt, (c) interactive codex tab with 30
sequential prompts in same session. Recommend a model + reasoning_effort
+ /fast for vocab classification + 60-100 word natural language output."
sleep 30
./tools/poll.sh enricher 80
```

Record codex's answer here BEFORE writing dispatch script:
> [TO BE FILLED IN at 5/15+ — paste codex's reply]

### Rule 4 — Cost arithmetic

Remaining D-1 cids = 38,295 total - 30,066 d1_results.jsonl = **~8,229 cids**.

**If batch=30 cids per codex call (recommended pattern)**:
- 8,229 / 30 = **~275 batches**
- per batch: ~12K codex overhead + 30 × ~1.5K input + 30 × ~200 output = ~63K tokens
- total: 275 × 63K = **~17.3M tokens**
- Pro plan ~2B weekly → **~0.9% weekly burn**

**If subprocess.run-per-cid (legacy pattern, DO NOT USE)**:
- 8,229 calls × 12K overhead = ~99M overhead alone
- **~5% weekly burn just on overhead**

→ Use batch pattern. Confirm with user if cost projection > expected.

### Rule 1 — Smoke ladder

```bash
# N=10 first
python3 -m tools.dispatch_enrich_batch --stage d1 --tab enricher \
    --batch-size 30 --max-batches 1 --smoke

# Measure: tokens / cid (read codex /status delta), spot-check 3 random rows
# for vocab compliance + visual_description quality (60-100 words, no fluff).

# N=100 (after N=10 PASSED)
python3 -m tools.dispatch_enrich_batch --stage d1 --tab enricher \
    --batch-size 30 --max-batches 4

# Measure tokens/cid, extrapolate full cost.
# If matches Rule 4 projection ± 20% AND quality OK → proceed to N=full.
# If diverges → STOP, escalate.

# N=full (after N=100 PASSED + user approval)
nohup python3 -m tools.dispatch_enrich_batch --stage d1 --tab enricher \
    --batch-size 30 \
    > logs/d1_resume_full.log 2>&1 &
```

---

## Pre-launch dependencies (must be done BEFORE this dispatch)

1. `tools/dispatch_enrich_batch.py` `_looks_idle()` bug FIXED (Phase 17
   Step 1). Currently the script hangs ~16 min before timeout because
   it doesn't reliably detect codex's idle prompt return.

2. `tools/quota_check.sh` exists (Phase 16 Step 5).

3. Codex tab (DB-ENRICHER) is up and responsive — verified via /status.

---

## Acceptance per smoke step

### N=10
- 10 jsonl rows appended to data/canonical/d1_results.jsonl
- 0 vocab violations (all program/style/color_tone/atmosphere ∈ enums)
- visual_description 60-100 words on average
- tokens / cid reported in stdout

### N=100
- 100 jsonl rows
- ≤ 5 vocab violations
- tokens / cid within 20% of N=10 measurement
- extrapolated full cost matches Rule 4 ± 20%

### N=full
- ~8,229 jsonl rows added (resume-aware, skips already-done cids)
- /status weekly burn < 2%
- Total wallclock < 4h

---

## Failure modes + handling

| Symptom | Action |
|---|---|
| `usage limit; sleeping Xs` in dispatch_enrich_batch output | Script auto-retries after sleep. If sleep > 1h, kill script and check /status manually; user decision. |
| `_looks_idle()` hang > 5 min for a single batch | Phase 17 Step 1 bug fix did not work — escalate, do not continue. |
| Vocab violations spike > 10% | Prompt template degraded; STOP, dispatch DB-ENRICHER to spot-check the prompt. |
| Cost arithmetic off by > 50% | Re-investigate Rule 3 codex pattern — may need different batching strategy. |

---

## Post-completion

```bash
# Update REPORT.md with new D-1 coverage
# Append handoff: ENRICH-DONE: d1_resume_v1 (cids=8229 tokens=~17M weekly_burn=~0.9%)
```

Then proceed to Phase 17 Step 4 (F-stage assembly).

---

## Self-review checklist (before final dispatch)

- [ ] Rule 2 /status check appended above with actual weekly%
- [ ] Rule 3 codex pre-investigation answer pasted above
- [ ] Rule 4 cost math confirmed (or revised based on Rule 3 answer)
- [ ] Rule 1 smoke ladder: N=10 + N=100 results recorded
- [ ] User approved if any step's measurement diverged from this plan
