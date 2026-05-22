# Job: canonical-v2-completeness-c4-apply

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C4
status: complete

## Scope

write_scope: `tools/canonical_v2_apply_completeness_c4.py`,
`data/canonical/country_conflict_refresh/*completeness_c4*`,
`data/reports/*completeness_c4*`, `.claude/ops/jobs/`, `.claude/Task.md`,
`.claude/REPORT.md`

inputs:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c3.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3.json`
- `data/reports/canonical_v2_remaining_review_verdict.json`

outputs:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c4.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4_affected.json`
- `data/canonical/country_conflict_refresh/completeness_c4_affected_cids.json`
- `data/reports/canonical_v2_completeness_c4_apply_report.json`

## Goal

Apply the 15 verified remaining `project_year` updates, validate the new
artifact family, then upsert only affected rows to Neon.

## Cost Arithmetic

No LLM batch work launched.

```
15 field updates x local patch/validation
= 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Do not overwrite C3 or resume10 artifacts.
- Do not touch `upload/`.
- Do not mutate R2 or legacy production tables.
- Apply only the 15 verified project-year candidates from
  `canonical_v2_remaining_review_verdict.json`.

## Result

Artifact patch command:

```bash
python3 tools/canonical_v2_apply_completeness_c4.py
```

Patch result:

- planned update CIDs: 15
- affected CIDs: 15
- strict field updates:
  - `project_year`: 15
- embedded field updates:
  - `project_year`: 15
- writes: new artifact files only

Validation:

- strict QC: PASS
  - report: `data/reports/canonical_strict_qc.completeness_c4.json`
- upload validator: PASS
  - report: `data/reports/canonical_v2_upload_dry_run.completeness_c4.json`
  - rows: 39,776
  - unique PKs: 39,776
  - failures: 0
- gap inventory:
  - report: `data/reports/canonical_v2_gap_inventory.completeness_c4.json`
  - review-needed items: 73, down from 88
  - description year candidates: 30, down from 45
  - location_full candidates: 43, unchanged
  - missing `project_year`: 1,304, down from 1,319
  - missing `location_city`: 2,012, unchanged

Neon affected-row upsert:

```bash
python3 tools/canonical_v2_neon_loader.py --upsert --confirm-db-write \
  --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4_affected.json \
  --report data/reports/canonical_v2_neon_loader_upsert_completeness_c4_affected.json
```

Neon result:

- rows loaded in transaction: 15
- row mapping failures: 0
- total rows: 39,776
- unique PKs: 39,776
- publishable/nonpublishable: 39,736/40
- missing embedding: 0
- `needs_image_derived_backfill`: 0
- writes: committed

## Completion

C4 is complete. C3 and resume10 artifacts remain untouched. Completeness C4
artifacts and Neon `canonical_v2_buildings` now include 15 additional
`project_year` improvements. R2 and legacy production tables were not mutated.
