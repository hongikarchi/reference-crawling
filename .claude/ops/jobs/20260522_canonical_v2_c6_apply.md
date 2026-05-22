# Job: canonical-v2-c6-apply

created: 2026-05-22 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C6.4
status: complete

## Approval

User approved:

`승인: C6.4 bld_038824.project_year=1989 canonical artifact 적용 및 Neon affected-row upsert 진행`

## Scope

write_scope:

- `tools/canonical_v2_apply_completeness_c6.py`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c6.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c6.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c6_affected.json`
- `data/canonical/country_conflict_refresh/completeness_c6_affected_cids.json`
- `data/reports/canonical_v2_completeness_c6_apply_report.json`
- `data/reports/canonical_strict_qc.completeness_c6.json`
- `data/reports/canonical_v2_upload_dry_run.completeness_c6.json`
- `data/reports/canonical_v2_gap_inventory.completeness_c6.*`
- `data/reports/canonical_v2_neon_loader_upsert_completeness_c6_affected.json`
- `.claude/ops/jobs/`, `.claude/Task.md`, `.claude/REPORT.md`

## Goal

Apply exactly one approved local completeness update:

- `bld_038824.project_year = 1989`

Then validate the new artifact family and upsert only the affected row to Neon
`canonical_v2_buildings`.

## Cost Arithmetic

No LLM/API batch work.

```
1 canonical row x local artifact update + validation + Neon affected-row upsert
= 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Do not mutate `upload/`.
- Do not run any `upload/*.py`.
- Do not mutate R2 or legacy tables.
- Neon mutation limited to affected-row upsert for one CID.
- C6 web-derived candidates remain blocked until exact-source review.

## Result

completed: 2026-05-22 KST

- apply status: PASS
- affected CIDs: 1
- applied update:
  - `bld_038824.project_year = 1989`
- strict artifact:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c6.json`
- embedded artifact:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c6.json`
- affected-row subset:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c6_affected.json`
- strict QC: PASS
- upload validator: PASS
- post-C6 gap inventory: PASS
  - review-needed items: 72
  - missing `project_year`: 1,303
- Neon affected-row upsert: PASS
  - rows loaded: 1
  - unique PKs in transaction: 39,776
  - missing embedding: 0
  - `needs_image_derived_backfill`: 0
- reports:
  - `data/reports/canonical_v2_completeness_c6_apply_report.json`
  - `data/reports/canonical_strict_qc.completeness_c6.json`
  - `data/reports/canonical_v2_upload_dry_run.completeness_c6.json`
  - `data/reports/canonical_v2_gap_inventory.completeness_c6.md`
  - `data/reports/canonical_v2_neon_loader_upsert_completeness_c6_affected.json`
