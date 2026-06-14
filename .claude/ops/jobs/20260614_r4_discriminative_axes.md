# R4 — new discriminative axes (era/scale/structural_system/roof_type/facade_pattern)

**Date:** 2026-06-14 · **Result:** SHIPPED (Neon deploy PASS, QC benchmark 10 PASS) · **Operator:** Claude (single-operator pipeline)

## Scope

make_web algo-support request R4: add 5 vocab-gated discriminative axes to
`canonical_v2_buildings` (39,478 rows) + extend the 3 tag precompute tables
(R1-R3, deployed 2026-06-12) from 6 → 11 axes. Planned + run end-to-end through
deploy in one session.

## Inputs / decisions (gates)

- **G0 vocab** (user-approved): SCALE(5)/STRUCTURAL_SYSTEM(7)/ROOF_TYPE(8)/
  FACADE_PATTERN(8) frozensets added to `core/vocab.py`; `era` excluded
  (derived from `project_year`). `Unknown` → `NULL`.
- **G2 vision** (conditional pre-approval): proceed if cost reasonable + roof
  Unknown drops meaningfully. Met.
- **G3 quality** (user review): ≥90% approve per axis on N=200. PASS (all 4).
- **G4 labels** (user): 34 ko/en labels approved as-is.
- **G5 Neon write** (user): "지금 배포" — approved.

## Method

- **era**: deterministic from `project_year` buckets (zero LLM).
- **text pass**: all 39,478 rows, codex `gpt-5.5` (low effort) primary, headless
  `claude -p` Haiku 4.5 fallback when codex quota exhausted. Batched claude (40
  items/call) to amortize ~18.7k-token agent boot → ~470 tok/item.
- **vision pass**: cover images, 3 visually-resolvable axes
  (roof/structural/facade). codex `exec -i` primary; **codex quota died until
  2026-06-19 → ran entirely on batched `claude -p` Haiku via the Read tool**
  (8 images/call, ~$0.0067/item, cwd=/tmp + append-system-prompt to avoid repo
  CLAUDE.md hijack). 30,622 ok / 57 dead-URL skips.
- **merge** (`tools/r4_axis_merge.py`, single source of truth): roof=vision-wins,
  structural+facade=text-wins (vision fills gaps), scale=text-only. Policies
  empirically validated in G3 review on disagreement cases: facade text 90% vs
  vision 10%, structural text 86% vs vision 14%, roof vision 67% vs text 33%.
- **review app** (`tools/r4_review_app.py`): N=200 (150 random + 50 rare-tag),
  surfaces text-vs-vision side-by-side on disagreement rows so one human pass
  settles the merge policy; records accuracy split by source.

## Deploy (2 transactions — single mega-txn rejected: ALTER holds ACCESS
EXCLUSIVE until commit, would stall make_web readers)

- **Txn A** (`r4_deploy_neon.py --ddl --apply --confirm-db-write`): catalog-only,
  sub-second locks (`lock_timeout=5s` + 3 retries). 5× `ADD COLUMN IF NOT EXISTS`
  (nullable, no default → no table rewrite) + CHECK + indexes; widen 3 tag-table
  axis CHECKs 6 → 11. COMMIT.
- **Txn B** (`canonical_v2_tag_stats_build.py --build --with-r4 --confirm-db-write
  --corpus-version c23_final+matstrip+r4`): DML/MVCC, readers never blocked.
  backfill 39,478 rows → rebuild 3 tag tables at 11 axes → in-txn QC → single
  COMMIT (ROLLBACK on any FAIL). All QC PASS.

## Outputs

- `canonical_v2_buildings`: +5 columns. Publishable (36,673) non-NULL coverage —
  era 99.7% / scale 100% / structural 91.6% / roof 63.2% / facade 90.9%.
- `canonical_v2_tag_stats` / `_tag_centroids` / `_tag_vocabulary`: 11 axes,
  18,655 rows (18,621 → +34 new tags), `corpus_version c23_final+matstrip+r4`.
  34 new ko/en labels live, none `is_generic`.
- Reports: `data/reports/r4_smoke/{full_text_report,vision_report}.json`,
  `data/reports/r4_review/accuracy.json`, `data/reports/tag_stats_build_report.json`.
- Sidecars (gitignored): `data/canonical/r4_results.{text,vision,merged}.jsonl`.

## Cost

- text pass: codex quota + ~free claude fill (subscription).
- vision pass: **~$168 claude subscription** (codex unavailable). Within the
  user's ≥20% weekly-quota-remaining ceiling (weekly 44% used at final wave).

## Verification

- pytest: 93 pass. External QC benchmark: **10 PASS / 0 FAIL / 1 INFO** (no
  regression). Live Neon inspect confirms columns + tag tables + corpus_version.

## Files

New: `tools/r4_axis_{smoke,merge,enrich}.py`, `tools/r4_vision_enrich.py`,
`tools/r4_supervisor.sh`, `tools/r4_review_app.py`, `tools/r4_deploy_neon.py`.
Edited: `core/vocab.py`, `tools/canonical_v2_{neon_loader,upload_validator,
tag_stats_build}.py`, `tools/manual_review_workflow.py`,
`data/canonical/tag_vocabulary_labels.json`, `docs/MAKEDB_ALGO_SUPPORT_RESPONSE.md`.
