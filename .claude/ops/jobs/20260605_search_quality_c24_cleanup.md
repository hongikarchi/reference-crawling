# 2026-06-05 Search Quality C24 Cleanup

## Scope

Goal: improve make_web-facing search and facet quality with an artifact-first C24 cleanup. C23 final remains production truth; current Neon `archi_data` was used only as a live reference/baseline. `user_data` was excluded.

No full crawl, full LLM, full vision, R2 write, committed Neon write, destructive cleanup, or `core/vocab.py` edit was run.

## Inputs

- C23 embedded artifact: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c23_final.json`
- Manual review audit baseline: `data/reports/manual_review_20260605/db_audit.json`
- QC baseline: `data/reports/manual_review_20260605/qc_benchmark.json`
- Live Neon benchmark table: `canonical_v2_buildings` read-only

## Outputs

- C24 artifact: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c24_search_quality.json`
- Cleanup tool: `tools/search_quality_cleanup.py`
- Focused tests: `tests/test_search_quality_cleanup.py`
- Report: `data/reports/search_quality_20260605/search_quality_c24_report.json`
- Report markdown: `data/reports/search_quality_20260605/search_quality_c24_report.md`
- Material mapping: `data/reports/search_quality_20260605/material_mapping.c24.json`
- Search keyword sidecar: `data/reports/search_quality_20260605/canonical_v2_search_keywords.c24.jsonl`
- Material review sidecar: `data/reports/search_quality_20260605/material_unmapped_review.c24.jsonl`
- Upload validator: `data/reports/search_quality_20260605/c24_upload_validator.json`
- Full reaudit: `data/reports/search_quality_20260605/c24_full_reaudit.json`
- Live Neon QC baseline after C24 prep: `data/reports/search_quality_20260605/live_neon_qc_benchmark_after_c24_prep.json`
- Neon dry-run report: `data/reports/search_quality_20260605/c24_neon_dry_run_upsert.json`

## Method

Implemented deterministic, high-confidence material normalization:

- removed non-material noise from `material_visual`
- moved known architectural elements to `architectural_elements`
- collapsed common raw strings into a compact controlled material set
- wrote unknown/empty cases to a local JSONL review sidecar
- used `unspecified` only as an explicit fallback for rows with no controlled material after cleanup
- generated an artifact-side `search_keywords` JSONL index from name, architects, program, style, color, atmosphere, material, typology, architectural elements, source categories, and visual description

## Metrics

- rows: 39,478
- publishable rows: 36,864
- material noise rows: 9,606 before / 0 after
- distinct material labels: 19,037 before / 35 after
- publishable empty material rows after cleanup: 0
- material review sidecar rows: 5,381 total / 5,262 publishable
- publishable search keyword coverage: 100.0%
- keyword count avg/p10/p50/p90: 58.83 / 50 / 58 / 69
- visual description words avg/p10/p50/p90: 57.58 / 50 / 56 / 63
- controlled facet OOV counts: none
- program distinct values: 14
- style distinct values: 12
- color tone distinct values: 8
- atmosphere distinct values: 12
- typology primary distinct values: 35
- architectural element distinct values: 14

## Validation

- `python3 tools/search_quality_cleanup.py --limit 100 ...`: PASS
- `python3 tools/search_quality_cleanup.py`: PASS
- `python3 -m pytest`: 87 passed
- `python3 tools/canonical_v2_upload_validator.py --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c24_search_quality.json --report data/reports/search_quality_20260605/c24_upload_validator.json`: PASS, failures {}
- `python3 tools/canonical_v2_full_reaudit.py --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c24_search_quality.json --strict data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c23_final.json --report data/reports/search_quality_20260605/c24_full_reaudit.json --md data/reports/search_quality_20260605/c24_full_reaudit.md`: PASS, hard_total 0
- `python3 tools/canonical_v2_qc_benchmark.py --json data/reports/search_quality_20260605/live_neon_qc_benchmark_after_c24_prep.json --md data/reports/search_quality_20260605/live_neon_qc_benchmark_after_c24_prep.md`: live Neon baseline still FAILS R5 because C24 was not committed
- `python3 tools/canonical_v2_neon_loader.py --dry-run-upsert --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c24_search_quality.json --report data/reports/search_quality_20260605/c24_neon_dry_run_upsert.json --batch-size 500`: PASS, 39,478 rows loaded in transaction, 0 row mapping failures, writes rolled back

## Result

C24 search-quality artifact is ready for manual review and approval-gated upload. The live Neon table remains unchanged and still shows the old material-noise QC failure until a future explicit `--upsert --confirm-db-write` run is approved.
