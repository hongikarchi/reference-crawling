# 20260614 — Full census audit (structure + semantic labels)

## Scope
User request: exhaustive ("전수검사") audit of the production DB to confirm no problems.
Target = **live Neon `archi_data`** (what make_web serves), not stale local artifacts.
Two layers: 100% deterministic census (free) + suspect-directed LLM verification
(vision + source, sampled). Read-only throughout. Report-only (no fixes applied).

## Inputs
- Live Neon `canonical_v2_buildings` (39,478) / `_architects` (14,216) / 3 tag tables.
- `core/vocab.py`, `data/crawl/*.db` (source text), prior `db_quality_audit.md` (2026-05).

## Tools built (new, read-only)
- `tools/audit_full_census.py` — deterministic layers L1-L4, L6, L7a, L7b + diag + prior-14.
- `tools/audit_label_sample.py` — suspect-directed stratified sampler (S1-S5) + source-evidence resolver.
- `tools/audit_fetch_images.py` — cover download + validate + downscale (manifest).
- Workflows (tiered Haiku/Sonnet/Opus): S1 typology↔program (200), vision re-judge (180).

## Outputs
- `data/reports/full_census_audit_2026Q2.{md,json}` — main report + deterministic results.
- `data/reports/qc_benchmark_2026Q2.json` — 11-rule benchmark (10 PASS / 0 FAIL).
- `data/reports/audit_s1_verdicts.json` (200 typology corrections),
  `audit_vision_verdicts.json` (180 vision verdicts), `audit_label_sample.full.json`,
  `audit_image_manifest.full.json`.

## Result
- **Structure CLEAN** (100% census): PK/CHECK/NULL/embedding/referential/tag-tables all
  pass; 11-rule benchmark 10 PASS/0 FAIL (no regression); R4 confirmed populated.
  Prior-14 (2026-05) mostly fixed (year hallucinations gone, name="test" gated).
- **Semantic label findings** (the user's concern):
  - F1 `typology_primary`: ~2,783 crosswalk mis-picks; 95% of N=200 sample real errors,
    80% typology-side. Fix = crosswalk re-rank.
  - F2 `style`: 64.6% "Contemporary" (systemic over-generality; vision 0% per-row wrong).
  - F3 `material_visual`: 28% contradict image / 31% non-material noise (advisory-grade).
  - F4 facade_pattern ~7%; baseline materially-wrong ~1.7% (DB sound).
- Minor/INFO: `updated_at` uniform (stale freshness signal); `year_kind` 2026 convention;
  cdn_url/blurhash 100% NULL. S4 leaked-name regex over-flagged (mostly legit names) →
  web grounding skipped as unwarranted.

## Cost
Deterministic layers free. LLM: S1 ~215k tok (4 Sonnet batches) + vision ~4.2M tok
(180 Haiku + Opus-on-flag) + smokes ≈ **~$15-20 total** (well under the ~$45-115 approved;
vision Haiku + Opus-only-on-flag kept it cheap). Smoke-laddered (N=10 → full) per axis.

## Follow-up (user-gated, NOT done)
Re-enrich typology crosswalk (F1), style prompt/vocab (F2), material noise vocab (F3);
re-upsert to Neon. No schema change needed. `audit_s1_verdicts.json` seeds F1 fixes.
