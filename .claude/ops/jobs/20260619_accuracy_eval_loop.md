# 20260619 — DB accuracy evaluation + goal-driven typology improvement loop

Follow-on to the 2026-Q2 census audit (`4038bd2`). User asked for a QUANTIFIED accuracy
metric (structure + content) and a goal-driven loop to raise it to a target.

## Method (two-tier, baseline-before-targets)
- **Tier 1 conformance** (deterministic, free): `tools/accuracy_metrics.py` → vector.
- **Tier 2 veracity** (sampled): representative stratified sample (`tools/accuracy_sample.py`,
  source×tier×era), independent **vision via subagent Read** (`accuracy-vision` workflow,
  Haiku batch agents), deterministic compare (`tools/accuracy_compare.py`), LLM adjudication
  of BOTH agree+disagree (`accuracy-adjudicate` workflow), estimator with Monte-Carlo CI +
  net-of-regression ceiling (`tools/accuracy_score.py`).

## Key findings (N=100/375 baselines)
- **Tier 1 = GREEN**: OOV 0, invariants 0, contradiction 2.45%, vagueness 0.16. New minor:
  507 `year_kind` time-drift rows.
- **Judge reliability (test-retest) caps measurability.** Vision self-agreement:
  material 90→97%, roof 80→85% (RELIABLE); scale 54→65%, structural 68 (UNRELIABLE — structure
  hidden in photos), facade 57→46% (UNRELIABLE). Multi-image probe proved upgrade does NOT
  rescue structural/facade. → scale/structural/facade are **conformance-only** (not veracity-
  measurable). `accuracy_judge_reliability_2026Q2.json`.
- **Tier 2 veracity (measurable axes):** typology 90.4% agree-w/source / 80% strict / **9.3%
  real error** (Sonnet source-text, N=375); roof 76% (cover-vision); material 75% (advisory).
- **Circularity caught (advisor):** baseline judge = fix source = holdout judge (all Sonnet
  source-text) → must verify with an INDEPENDENT judge. **Opus blind A/B** calibration on 34
  `stored_wrong` calls → **91% precision** (3 regressions, all Pavilion over-calls → fixed by a
  "Pavilion is a form not a use" rule). Calibrated true error ~8.5%.

## Gate decisions (user)
- Loop scope: **typology (primary) + roof/material (secondary)**; style excluded (no GT);
  scale/structural/facade conformance-only.
- Targets (Moderate): typology real-error **≤5%**, roof/material ≥85%.
- Typology fix: **full Sonnet population re-derive from source descriptions** (~$50-60 approved).

## Typology fix (IN PROGRESS)
- Queue: `tools/typology_rederive_queue.py` → 34,003 publishable rows w/ description.
- Re-derive: `typology-rederive-pop` workflow (Sonnet, pre-split per-batch files → ~2.8s/row;
  refined prompt w/ Pavilion rule + tie-leniency), 681 batches across 4 parallel shards.
- Corrections: `tools/typology_corrections.py` (Pavilion double-guard).
- **Net-validation + holdout = Opus blind A/B** (`tools/typology_ab_build.py`), NOT the Sonnet
  fixer → closes circularity. Holdout = fresh disjoint sample (`accuracy_sample.py --seed
  --exclude-from`).
- Apply = artifact-only this session; **Neon write user-gated** (Phase 2).

## OUTCOME — COMPLETE (2026-06-21, user-gated writes approved)
- **Re-derive ran 681/681 batches** (33,999 rows; session-limit interruptions resumed via
  explicit-index re-run). **1,647 corrections** (4.84%; Pavilion double-guard dropped 198).
- **Net-validation (Opus blind A/B, 44 sample): 92% precision, +77% net** — consistent with
  the 91% calibration.
- **Independent Opus holdout (187 disjoint rows): real error 2.7% → 1.6%**, 2 confirmed errors
  fixed, 0 new errors. Strict-exact ~79%.
- **KEY FINDING:** the initial Sonnet judge's 9.3% was an over-strict artifact (counted
  near-synonyms + Pavilion-form confusions as errors). Independent Opus shows clear error was
  already 2.7% — **DB already met the ≤5% target before the fix**; the re-derive was a precision
  refinement, not an error-fix. Two-judge calibration + independent verification caught this.
- **Applied to Neon (approved):** `apply_typology_corrections_neon.py --confirm-db-write` →
  1,647 rows, `typology_primary_source='descr_rederive_2026q2'`, typology_tags reconciled
  (primary∈tags 0 viol, OOV 0). Architect refresh committed (740). **Benchmark 10 PASS/0 FAIL.**
- **Contradiction proxy rose 2.45%→2.68%** (828→906): more-precise typology vs the COARSE,
  not-re-derived program + the metric's narrow Civic-Building acceptable-set. Spot-check
  confirmed corrections are good; residual is program-side (out of typology scope).
- **Durability:** `dump_overrides_from_neon.py` → full override sidecar (39,478) +
  `apply_overrides_to_artifact.py` → c26 re-synced (validator PASS, 0 failures);
  `dump_changed_sidecar.py` → changed-only sidecar 24,792. Re-upsert + c11-rebuild safe.
- Reversible: old values in `typology_corrections_descr.jsonl`.

## Cost
~3.7M tokens baseline+calibration + ~27M Sonnet re-derive + ~2M Opus validation ≈ **~$70-90**
(within $100 cap). One session-limit hit mid-re-derive (resumed next window). Haiku rejected
as fixer (47% recall). Tiered Haiku-flag→Sonnet-confirm rejected (recall too low).

## Artifacts
`data/reports/accuracy_eval_2026Q2.{md,json}`, `accuracy_judge_reliability_2026Q2.json`,
`accuracy_tier1.json`, `accuracy_tier2.n100.json`; tools `accuracy_{metrics,sample,compare,
score,adjq,fetch_multi}.py`, `typology_{rederive_queue,corrections,ab_build}.py`.
