# Dispatch plan: D-2 cover Vision enrichment full run (5/15+ Phase 17 Step 3)

**Target tab**: DB-ENRICHER
**Stage**: D-2 (cover image → style/color_tone/material/visual_description)
**Created**: 2026-05-09 (Phase 16 prep)
**Executes**: 2026-05-15 21:24 KST or later (after weekly quota reset)

---

## Pre-launch checks (Rule 1, 2, 3, 4)

### Rule 2 — `/status` quota check

Same as d1_resume.md. Weekly ≥ 95%.

### Rule 3 — Codex pre-investigation (Vision-specific)

```bash
./tools/dispatch.sh enricher "Codex CLI 0.129 Vision (codex exec -i FILE):
what's the most token-efficient pattern for processing 28K building cover
images that each need a JSON answer (style/color_tone/material[]/desc 60-100
words)? Compare: (a) one image per codex exec, (b) batched but vision
typically takes single image — is multi-image attach supported? (c) GPT-5.5
vs GPT-5.5-mini for visual reasoning quality. Recommend model + reasoning
+ /fast for this task."
sleep 30
./tools/poll.sh enricher 80
```

> [TO BE FILLED IN at 5/15+]

### Rule 4 — Cost arithmetic

Remaining D-2 = 38,295 total - 9,670 valid = **~28,625 cids**.

**Vision per-image (likely confirmed by Rule 3)**:
- per call: ~12K codex overhead + image (~2-3K vision tokens) + ~250 output = ~15K tokens
- 28,625 × 15K = **~430M tokens**
- Pro plan ~2B weekly → **~22% weekly burn**

⚠️ Rule 4 user-approval threshold = 25%. This is at 22% — user MUST be
notified before launch and confirm OK.

**Alternative: gpt-5.5-mini (if Rule 3 says quality OK)**:
- ~5x cheaper input → ~3K overhead + 1K image + 250 output = ~4-5K
- 28,625 × 5K = **~143M tokens = ~7% weekly burn**

→ Strongly prefer mini if Rule 3 confirms quality.

### Rule 1 — Smoke ladder (Vision-aware)

```bash
# N=10 with full gpt-5.5
python3 -m tools.dispatch_enrich_batch --stage d2 --tab enricher \
    --batch-size 1 --max-batches 10 --smoke

# Spot check 3 random outputs:
#   - style_image ∈ STYLE enum?
#   - color_tone_image ∈ COLOR_TONE enum?
#   - material_visual_image is non-empty list of plausible materials?
#   - visual_description_image 60-100 words, concrete sensory description?

# If gpt-5.5 OK → try N=10 with gpt-5.5-mini
# (Rule 3 must have approved this combo)

# Compare quality side-by-side (3 random rows each).

# N=100 with chosen model
# N=full only after user approval given Rule 4 burn projection
```

---

## Pre-launch dependencies

1. `tools/dispatch_enrich_batch.py` `_looks_idle()` FIXED (Phase 17 Step 1).
2. `tools/dispatch_enrich_batch.py` supports `--stage d2` properly
   (already confirmed by tests).
3. e1_clusters.jsonl complete (38,295 rows ✓ done in Phase 15).

---

## Acceptance per smoke step

### N=10
- 10 d2_results.jsonl rows with NON-NULL payload (style_image etc.)
- Spot check: 3/3 plausible material/desc
- tokens / cid within 20% of Rule 4 projection

### N=100
- 100 rows valid
- ≤ 5 nulls (Vision API edge cases acceptable at < 5%)
- Cost extrapolation matches projection

### N=full
- ~28,625 rows added
- /status weekly burn < 25% (or whatever model chosen)
- Wallclock 1-3 days (Vision is slower than text)

---

## Concurrency option

D-1 + D-2 can run on different tabs concurrently:
- D-1 on DB-ENRICHER
- D-2 on DB-MATCHER (idle, or spawn DB-ENRICHER-2 workspace)

**Decision (defer to 5/15+)**: depends on Rule 2 quota reading. If
weekly ≥ 95% and Rule 3 confirms gpt-5.5-mini, can run all three (D-1,
D-2, E-2) concurrently.

---

## Failure modes

| Symptom | Action |
|---|---|
| Vision call returns empty payload | retry once with explicit prompt; if still empty, log to d2_failures.jsonl |
| `usage limit; sleeping` | auto-retry; if > 1h, escalate |
| Quality 차이 (mini vs full) > 30% | revert to gpt-5.5 full, accept higher cost |
| Cost burn > Rule 4 projection × 1.5 | STOP after 1000 cids; recompute |

---

## Post-completion

Update REPORT.md, append handoff:
> `ENRICH-DONE: d2_full_v1 (cids=28625 tokens=~143M-430M weekly_burn=~7-22%)`

Proceed to Phase 17 Step 4 (F-stage assembly with full D-2).

---

## Self-review checklist

- [ ] Rule 2 /status check appended
- [ ] Rule 3 codex Vision pre-investigation answer pasted
- [ ] Rule 4 cost math finalized (gpt-5.5 vs mini decision)
- [ ] Rule 1 smoke N=10 + N=100 PASSED
- [ ] User approved 22% weekly burn (or alternative model approved)
