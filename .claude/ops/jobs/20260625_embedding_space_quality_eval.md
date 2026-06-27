# 20260625 — Embedding-space quality eval (Tier-1, measurement-only)

Plan: `~/.claude/plans/shimmering-discovering-quasar.md` (approved).
Goal: measure whether the recommender's 384-dim coordinate matches human taste, and
whether a visual coordinate beats it. **All local artifacts + reports. Zero Neon
writes. No make_web change.** make_web algorithm untouchable → evidence only.

## Status: Phase 0 foundation DONE (free, no LLM yet)

### Built
- `tools/neighbor_eval_prep.py` — streams the 1 GB
  `canonical_buildings_strict_embedded.completeness_c26_rem2026q2.json`
  (`{"buildings":[...]}`, one line) once → compact sidecar in
  `data/reports/neighbor_eval/`: `embeddings.npy` (float32, L2-normalized),
  `meta.jsonl` (id + cover + strata + display fields), `manifest.json`.
- `tools/neighbor_eval.py` — offline cosine top-K (dot==cosine, brute-force matmul,
  sub-second at 36,673×384) + `--peek` free no-LLM neighbor view.

### Verified
- Full extract: **36,673 publishable rows** kept (== documented production
  publishable count ✓), 2,805 non-publishable skipped (39,478−36,673 ✓),
  0 bad/non-finite embeddings, all norms ≈ 1.0. 13 s.
- `--peek` sanity: current text-embedding neighbors are **tightly program/typology/
  style-clustered** (Housing→Housing, school→schools, Museum→Museum). Coherent, but
  possibly *too* program-locked — exactly the hypothesis the image-embedding A/B tests
  (cross-program visual taste). Confirms the cosine pipeline end-to-end.
- Fixes: meta.jsonl split on `\n` only (str.splitlines breaks on U+2028/29 in fields);
  `np.errstate` to silence numpy2/Accelerate spurious matmul warnings (data clean).

## BLOCKER recorded
- **Swipe cross-check (advisor-requested) needs `user_data` DB access, which make_db
  does NOT have.** `.env` = archi_data writer + R2 only; `.env.make-web` = read-only
  on canonical_v2_* (archi_data). `SwipeEvent` lives in make_web's separate `user_data`
  Neon DB. → cross-check deferred until make_web team grants user_data read (or a swipe
  export). App may also be pre-launch w/ little swipe volume. LLM taste-coherence judge
  remains the primary, as planned.

## Phase 1 — N=10 SMOKE DONE (text baseline)
- `tools/neighbor_eval_judge.py` (build-queue + judge). Judge = Opus via `claude -p`
  CLI (repo auth path; `ANTHROPIC_API_KEY` unset so SDK direct unavailable),
  `--output-format json` → real `total_cost_usd`. Taste-coherence prompt, blind
  (images only, no stored labels), candidates shuffled-label.
- **Result (TEXT embedding baseline): mean precision@5 = 0.56**, 10 seeds across 10
  programs, 0 errors. Per-seed 0.4-0.8.
- **Key qualitative finding:** high-cosine neighbors (rank-1/2, cos ~0.88-0.91) get
  rejected for **character/mood mismatch** — "playful curvy color, different mood" /
  "utilitarian gritty, not serene minimal" / "light airy white, not raw" / "mundane
  platform, no drama". Confirms the hypothesis: text embedding pulls same-program/
  type close but they differ in **visual feel** — ~44% of top-5 = "right category,
  wrong vibe". Exactly what a visual coordinate (②) might fix.
- **Caveat:** N=10, single judge, NO calibration yet → 0.56 is provisional. Needs
  test-retest + human spot-check before trusting (plan rule).

## COST (measured)
- **$0.344/seed** (Opus/CLI): ~$0.15 Claude-Code framework overhead + ~$0.19 vision
  (6 imgs). N=10 = $3.44.
- Projection at this rate: N=100 ≈ $34/space; full ~300 ≈ ~$100/space. Phase 1+2
  (baseline + 3 ② branches, smoke@100 then winner@full) ≈ **$300-450**. Too high.
- **Optimization levers (full-run, user decision):** (1) `ANTHROPIC_API_KEY` → SDK
  direct kills framework overhead (~-45%) AND unlocks **Batches API (-50%)** → biggest
  win; (2) Sonnet bulk + Opus spot-check (tiering memory, ~-40%); (3) smaller full
  sample ~150-200 (fine for a rate). Combined could drop Phase1+2 to <$50.

## Phase 2 — ② directional A/B DONE (text vs pixel, paired N=30)
Shared 1500-pool (`data/reports/neighbor_eval/pool/`, 1502 tried / 2 cover-fails),
3 aligned matrices: text(384), DINOv2(768), SigLIP(768) via
`tools/neighbor_eval_visual.py` (MPS, free). Same 30 seeds, same Opus judge, paired.

**RESULT — visual coordinates BEAT text, significantly:**
| space | mean precision@5 | vs text (win/tie/loss) | sign-test p |
|---|---|---|---|
| TEXT (current prod) | 0.52 | — | — |
| DINOv2 (pure pixel) | 0.667 (+0.147) | 18 / 5 / 7 | 0.043 |
| **SigLIP (img-text)** | **0.693 (+0.173)** | **19 / 8 / 3** | **0.001** |

- **SigLIP wins** (0.52→0.69 = +33% relative taste-coherence), significant at N=30.
- Pixel fixes text's **program-drift** cases (Housing T=0.2→S=1.0, Museum T=0.4→D=1.0).
  Text wins only a few (Healthcare/Office/Public — where program-match IS the taste).
- **Anti-bias check:** SigLIP (semantic img-text) > DINOv2 (pure pixel). If the vision
  judge were merely rewarding raw visual similarity, pure-pixel DINOv2 would win — it
  doesn't. → judge is NOT just scoring appearance (addresses the advisor concern).
- Cost: **$30.53** for 90 Opus/CLI calls (in the $30-50 target).

**Caveats:** N=30, single judge; 1500-pool not full 36k; upstream of MMR/K-Means.

## Phase 2b — CONFOUND TEST overturns the "visual wins" claim
Advisor caught a modality **home-advantage**: visual neighbors were *selected* by
vision and *graded* by a vision LLM reading the same pixels — "judge character not
appearance" is uninstructable (same pixels, same model). The vision-judge A/B gave
visual a home game. Discriminating test: re-judge the SAME queues with a **TEXT-ONLY
judge** (descriptors, no images, Sonnet — `tools/neighbor_eval_textjudge.py`), biased
toward text. Near-dup inflation separately ruled out (rank-1 name-overlap = generic
"House" tokens, not same-building; 2/30 visual).

**Result — the visual win was largely an artifact:**
| space | VISION judge | TEXT judge | AVG (fair) |
|---|---|---|---|
| text (prod) | 0.520 | 0.587 | 0.553 |
| dinov2 | 0.667 | 0.527 | 0.597 |
| siglip | 0.693 | **0.473** | 0.583 |

- Each space wins under its **own** modality's judge (home game). SigLIP **collapses**
  0.693→0.473 (below text's 0.587) under the text judge.
- Under TEXT judge: text BEATS both (siglip 6w/15l p=0.078; dinov2 5w/12l p=0.143).
- **Fair average (both judges):** siglip vs text +0.030, **p=0.66 (TIE)**; dinov2 vs
  text +0.043, p=0.043 (weak, within N=30 + multiple-comparison noise).

**VERDICT: INCONCLUSIVE — no coordinate clearly beats text once the judge-modality
confound is controlled.** The "+33% SigLIP wins" was a measurement artifact. Do NOT
deploy a visual coordinate on this evidence. A modality-neutral resolution needs real
**swipe data** (revealed taste) — loops back to launch→collect→decide. Branch B
(recaption) would face the same confounded instruments → not worth running now.

**What survives (judge-independent):** text embedding precision is only ~0.52-0.59
(both judges) — genuine room to improve, but via better text fields, not a proven
modality switch. The qualitative "program-drift / wrong-vibe neighbors" is real.

## Phase 3 — COVER-IMAGE QUALITY AUDIT DONE (judge-independent)
`tools/cover_quality_audit.py` — Haiku vision, batched (8/call, $0.01/cover),
classifies each publishable cover into a representativeness vocabulary. Sample n=150
random publishable. **Validated:** 4/4 visual spot-check (I Read the covers) — Haiku
reliable on the good/bad BINARY (render/interior/exterior calls correct); fine
sub-category noisy (one landscape labelled poor_quality vs not_a_building). So the
good-cover RATE is trustworthy; sub-category mix is directional.

**Result (n=150): good_cover_rate = 0.47 → ~53% of covers are NOT ideal swipe cards.**
| category | rate | |
|---|---|---|
| representative_exterior | 44% | GOOD |
| aerial_or_siteplan | 3% | GOOD |
| **interior_only** | **25%** | the big problem — quarter show only inside |
| detail_closeup | 13% | zoomed fragment |
| render_or_drawing | 7% | CGI, not photo |
| people_or_object_dominant | 5% | |
| poor_quality / not_a_building | 3% | |

Per-program good-rate varies hugely: Office 69%, Housing 55%, Public 53%, **Museum 30%,
Other 18%, Hospitality 10%** (hotels/restaurants lead with interiors).

**Actionable + cheap to fix:** this is a cover-SELECTION problem, not embedding. Many
buildings likely have a better exterior in their `all_images` array; `display_cover_url`
just picked a non-exterior. Remediation = re-pick cover from `all_images` preferring
exterior (run this classifier over `all_images`, or use `tools/cover_review_app.py`).
Updates `display_cover_url` only → re-upload, user-gated; NO make_web change.
Outputs: `data/reports/cover_audit/s150/{report.json,classified.jsonl,remediation.jsonl}`.

## Phase 3b — cover RE-PICK classifier validation (toward auto re-selection)
Goal: re-pick a better exterior cover from each building's `all_images`. Needs a
classifier to score candidates. Tested FREE local **SigLIP zero-shot**
(`tools/cover_classify_siglip.py`, MPS, $0) vs the Haiku-150 labels:
- exact-category 66%, **GOOD/BAD binary 78%** (both-good 59 / both-bad 58 / Haiku-bad-
  SigLIP-good 21 / Haiku-good-SigLIP-bad 12).
- Tiebreak (I Read disagreements): SigLIP **over-calls "exterior"** at low confidence
  (scores 0.02-0.08) — e.g. a brick-walled interior installation → SigLIP "exterior"
  (wrong), Haiku "detail" (right). One aerial-render genuinely borderline.
- **Verdict: SigLIP-alone is NOT reliable enough for final cover picks** (~22% binary
  error, lenient). Haiku more trustworthy on the binary but costs at scale.

**Re-pick design = open decision (cost vs reliability vs scale):** full census ~36.7k,
~53% need re-pick (~19.4k). Options: (a) SigLIP-only free, accept ~78%; (b) **hybrid**
— SigLIP shortlists exterior candidates free, Haiku confirms 1/building (~$150-200);
(c) scope to clearest wins only (interior_only 25%) where an exterior alt exists.
Also needs `all_images` extraction (not yet pulled — in 1GB source / Neon).

## Phase 3c — cover RE-PICK smoke WORKS (validated)
`tools/extract_all_images.py` → all_images.jsonl (36,673 blds, **median 18 candidate
imgs/bld**, 113 with ≤1). `tools/cover_repick.py` scores current cover + capped
candidates on a SigLIP exterior-vs-{interior,detail,render,landscape} softmax (free).
Smoke: 40 bad-cover blds, cap 6.
- **hit-rate 0.50** — half the bad covers have a notably-better exterior in all_images.
- **Eyeball-validated 2/2 dramatic re-picks** (I Read before/after): bld_017634
  interior-lobby (0.00) → stunning Paris Haussmann facade (0.92); bld_029377 forest-
  landscape (0.01) → the timber-slat chapel structure (0.92). Re-pick genuinely fixes
  bad covers using the building's OWN images.

**Full-census reality (the real constraint = DOWNLOAD volume, not $):**
- Classify 36.7k covers + re-pick ~53% bad × ~7 imgs each ≈ **150k+ image downloads
  = multi-hour overnight batch** (SigLIP compute is fast/free).
- Hybrid Haiku-confirm on the ~50% proposals (~9.7k) = batched ≈ **$20-50**.
- Net expected: **~9-10k covers improvable** corpus-wide.
- Output = reversible re-pick proposal sidecar → user review → update `display_cover_url`
  → re-upload (user-gated Neon write). NO make_web change.
- **Gated on user approval** (big job + cost + downstream Neon write).

## Total spend this session: ~$66 (vision A/B $30.5 + text judge $8.8 + N=10 $3.4
+ cover audit $1.2 + SigLIP/repick smokes ~free-$2). 8 reusable tools added; durable.

## Next (decisions)
- **Branch B (Claude-recaption→384-dim) still untested** — it's the deploy-WITHOUT-
  make_web-change option (SigLIP/DINOv2 are 768-dim → schema + make_web coordination).
  Worth a Round-2 test: does recaption capture the visual win in a 384-dim drop-in?
- Calibrate judge (test-retest + human spot-check) before trusting the absolute metric.
- Optional: confirm direction holds at full 36k pool.
- Phase 3 cover audit (sample for rate).
