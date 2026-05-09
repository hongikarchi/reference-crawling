# Dispatch plan: E-2 lightweight 5-type Vision (5/15+ Phase 17 Step 3)

**Target tab**: DB-ENRICHER (or new DB-ENRICHER-2 if concurrent with D-1/D-2)
**Stage**: E-2 (per-cid 5 best-image candidates → exterior/interior/drawing/aerial/detail)
**Created**: 2026-05-09 (Phase 16 prep)
**Executes**: 2026-05-15 21:24 KST or later

---

## Why lightweight rewrite

Original E-2 (tools/e2_vision_5type.py + image_dedup_5type.py) classifies
EVERY phash cluster's best image. With ~80 clusters per cid average,
38K cids × 80 = ~3M Vision calls — economically impossible.

**Lightweight** = per-cid 5-type quota:
- For each cid, pick top 5 candidate images (rank by cluster size +
  source priority + image_order=0)
- 1 Vision call per candidate → fill `covers_by_type` directly
- Skip filename-heuristic-classifiable images (drawing/aerial/section etc.)

Total: 38K × 5 = 190K Vision calls (vs 3M = 16x reduction).

---

## Pre-launch checks

### Rule 2 — `/status`

Same. Weekly ≥ 95%.

### Rule 3 — Codex pre-investigation

```bash
./tools/dispatch.sh enricher "We have 38K canonical buildings. Each has
~80 phash clusters of source images. Goal: classify ONE image per
type (exterior/interior/drawing/aerial/detail) per cid into a
covers_by_type dict. Filename heuristic catches drawing/aerial keywords;
remaining ~5 best candidates per cid need Vision. What's the most
token-efficient pattern? Can we batch 5 images in one codex exec call
('classify each of these 5 images as one of 5 types')? Or is it 1
image per call?"
sleep 30
./tools/poll.sh enricher 80
```

> [TO BE FILLED IN at 5/15+]

### Rule 4 — Cost arithmetic

Per cid: 5 Vision calls × ~5K tokens (mini) = ~25K tokens
Total: 38K × 25K = **~950M tokens** ❌ exceeds 25% threshold

**Optimization 1**: filename heuristic catches ~30% (drawing/aerial). 
Remaining: 38K × 5 × 0.7 = 133K calls × 5K = ~665M = ~33% weekly burn.

**Optimization 2**: combine 5 images in one call (if Rule 3 says supported)
38K × 1 call × 25K tokens (5 images attached) = ~950M, same.

**Optimization 3**: 5 → 3 type quota (skip "detail" + "aerial" if very
rare — they often default to filename heuristic anyway):
38K × 3 × 5K × 0.7 = ~400M = ~20% weekly burn ✓

→ Final approach contingent on Rule 3 answer. May need user approval
either way (≥ 25% threshold).

### Rule 1 — Smoke ladder

```bash
# N=10 with chosen approach
python3 -m tools.dispatch_enrich_batch --stage e2 --tab enricher \
    --batch-size 1 --max-batches 10 --smoke

# Verify: 10 e2_image_types.jsonl rows with covers_by_type populated
# (at least 'exterior' and one other type non-null)

# N=100
# N=full (after user approval per Rule 4)
```

---

## Pre-launch dependencies

1. `tools/dispatch_enrich_batch.py` `_looks_idle()` FIXED.
2. **`tools/e2_vision_5type.py` rewritten as lightweight** (Phase 17 Step 1).
   The current `tools/e2_vision_5type.py` per-cluster algorithm MUST be
   replaced with per-cid 5-quota.
3. e1_clusters.jsonl complete ✓.

---

## Acceptance per smoke step

### N=10
- 10 e2_image_types.jsonl rows
- covers_by_type: at least exterior populated for 10/10
- Filename heuristic skipped Vision call for any drawing/aerial

### N=100
- 100 rows
- ≥ 80% have ≥ 2 type covers populated
- Cost matches Rule 4 projection ± 30%

### N=full
- ~38K rows (all cids)
- Weekly burn matches projection
- Wallclock 6-12 hours

---

## Failure modes

| Symptom | Action |
|---|---|
| `_looks_idle` hang | Phase 17 Step 1 fix did not work — escalate |
| 5+ images per call not supported | Fall back to 1 call per type (5 calls per cid) |
| Quality of Vision answers unstable across types | Add few-shot examples to prompt |
| Burn > projection × 1.5 | STOP at 1000 cids, recompute |

---

## Post-completion

Update REPORT.md, append handoff:
> `MATCH-DONE: e2_lightweight_v1 (cids=38295 tokens=~400-700M weekly_burn=~20-35%)`

If E-2 burn pushes weekly past 50%, Phase 17 final F-stage rebuild may
need to wait until next weekly reset (5/22+).

---

## Self-review checklist

- [ ] Rule 2 /status check appended
- [ ] Rule 3 codex Vision multi-image investigation answer pasted
- [ ] Rule 4 cost math finalized (5 quota? 3 quota? batched?)
- [ ] Rule 1 smoke N=10 + N=100 PASSED
- [ ] User approved if burn ≥ 25%
- [ ] Lightweight rewrite of e2_vision_5type.py LANDED in Phase 17 Step 1
