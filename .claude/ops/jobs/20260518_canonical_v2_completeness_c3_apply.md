# Job: canonical-v2-completeness-c3-apply

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C3
status: complete

## Scope

write_scope: `tools/canonical_v2_apply_completeness_c3.py`,
`data/canonical/country_conflict_refresh/*completeness_c3*`,
`data/reports/*completeness_c3*`, `.claude/ops/jobs/`, `.claude/Task.md`,
`.claude/REPORT.md`

inputs:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict.resume10_complete.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`
- `data/reports/canonical_v2_review_rule_candidates.json`
- `data/reports/canonical_v2_llm_location_adjudication.json`

outputs:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c3.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3_affected.json`
- `data/canonical/country_conflict_refresh/completeness_c3_affected_cids.json`
- `data/reports/canonical_v2_completeness_c3_apply_report.json`

## Goal

Apply user-approved C3 completeness updates in two phases:

1. Create patched artifacts and validate them.
2. If validation passes, upsert only affected rows to Neon
   `canonical_v2_buildings`.

## Cost Arithmetic

No LLM batch work launched.

```
129 field updates x local patch/validation
= 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Do not overwrite resume10 complete artifacts.
- Do not touch `upload/`.
- Do not mutate R2 or legacy production tables.
- Apply only approved C2.5 `project_year` and C2.6 `location_city` candidates.
- Country-only location strings remain blocked from city.

## Result

Artifact patch command:

```bash
python3 tools/canonical_v2_apply_completeness_c3.py
```

Patch result:

- planned update CIDs: 129
- affected CIDs: 129
- strict field updates:
  - `project_year`: 91
  - `location_city`: 38
- embedded field updates:
  - `project_year`: 91
  - `location_city`: 38
- writes: new artifact files only

Validation:

- strict QC: PASS
  - report: `data/reports/canonical_strict_qc.completeness_c3.json`
- upload validator: PASS
  - report: `data/reports/canonical_v2_upload_dry_run.completeness_c3.json`
  - rows: 39,776
  - unique PKs: 39,776
  - failures: 0
- gap inventory:
  - report: `data/reports/canonical_v2_gap_inventory.completeness_c3.json`
  - review-needed items: 88, down from 217
  - description year candidates: 45, down from 136
  - location_full candidates: 43, down from 81
  - missing `project_year`: 1,319, down from 1,410
  - missing `location_city`: 2,012, down from 2,050

Neon affected-row upsert:

```bash
python3 tools/canonical_v2_neon_loader.py --upsert --confirm-db-write \
  --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3_affected.json \
  --report data/reports/canonical_v2_neon_loader_upsert_completeness_c3_affected.json
```

Neon result:

- rows loaded in transaction: 129
- row mapping failures: 0
- total rows: 39,776
- unique PKs: 39,776
- publishable/nonpublishable: 39,736/40
- missing embedding: 0
- `needs_image_derived_backfill`: 0
- writes: committed

## Completion

C3 is complete. Resume10 artifacts remain untouched. Completeness C3 artifacts
and Neon `canonical_v2_buildings` now include 91 `project_year` and 38
`location_city` improvements. R2 and legacy production tables were not mutated.
