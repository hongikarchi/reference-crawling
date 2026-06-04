# 2026-06-05 Search Quality C25 Cleanup

## Scope

Goal: continue artifact-first make_web search-quality cleanup from the C24 manual material queue. C23 final remains the production truth; C25 is a proposed artifact. Neon and R2 were not committed.

No full crawl, full LLM, full vision, R2 write, committed Neon write, destructive cleanup, or `core/vocab.py` edit was run.

## Inputs

- C23 embedded artifact: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c23_final.json`
- C24 unresolved material queue: `data/reports/search_quality_20260605/material_unmapped_review.c24.jsonl`
- C24 search-quality report: `data/reports/search_quality_20260605/search_quality_c24_report.json`
- Current live Neon baseline: read-only `canonical_v2_buildings`

## Outputs

- C25 artifact: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c25_search_quality.json`
- C25 report: `data/reports/search_quality_20260605/search_quality_c25_report.json`
- C25 report markdown: `data/reports/search_quality_20260605/search_quality_c25_report.md`
- C25 material mapping: `data/reports/search_quality_20260605/material_mapping.c25.json`
- C25 search keyword sidecar: `data/reports/search_quality_20260605/canonical_v2_search_keywords.c25.jsonl`
- C25 unresolved material queue: `data/reports/search_quality_20260605/material_unmapped_review.c25.jsonl`
- Upload validator report: `data/reports/search_quality_20260605/c25_upload_validator.json`
- Full reaudit report: `data/reports/search_quality_20260605/c25_full_reaudit.json`
- Live Neon QC baseline: `data/reports/search_quality_20260605/live_neon_qc_benchmark_after_c25_prep.json`
- Neon dry-run report: `data/reports/search_quality_20260605/c25_neon_dry_run_upsert.json`

## Method

Used the C24 review queue to add high-confidence deterministic classifications only:

- true materials mapped into compact material labels
- architectural terms moved to existing `architectural_elements`
- known landscape/display/furniture/lighting/context terms dropped from material review
- unresolved ambiguous terms kept in the C25 review queue
- rows with no controlled material keep explicit `material_visual: ["unspecified"]` for loader and facet stability

`core/vocab.py` was not edited.

## Metrics

- rows: 39,478
- publishable rows: 36,864
- material noise rows: 9,606 before / 0 after
- material labels: 19,037 before / 40 after
- publishable empty material rows after cleanup: 0
- C24 unresolved material queue rows: 5,381 total / 5,262 publishable
- C25 unresolved material queue rows: 4,145 total / 4,064 publishable
- unresolved queue reduction: 1,236 total / 1,198 publishable
- unspecified material rows: 5,102 total / 4,993 publishable
- publishable search keyword coverage: 100.0%
- keyword count avg/p10/p50/p90: 58.9 / 50 / 58 / 69
- visual description words avg/p10/p50/p90: 57.58 / 50 / 56 / 63
- controlled OOV counts: none

## Validation

- `python3 -m pytest tests/test_search_quality_cleanup.py`: 4 passed
- `python3 tools/search_quality_cleanup.py --limit 100 ...`: PASS
- `python3 tools/search_quality_cleanup.py`: PASS
- `python3 -m pytest`: 87 passed
- `python3 tools/canonical_v2_upload_validator.py --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c25_search_quality.json --report data/reports/search_quality_20260605/c25_upload_validator.json`: PASS, failures {}
- `python3 tools/canonical_v2_full_reaudit.py --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c25_search_quality.json --strict data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c23_final.json --report data/reports/search_quality_20260605/c25_full_reaudit.json --md data/reports/search_quality_20260605/c25_full_reaudit.md`: PASS, hard_total 0
- `python3 tools/canonical_v2_qc_benchmark.py --json data/reports/search_quality_20260605/live_neon_qc_benchmark_after_c25_prep.json --md data/reports/search_quality_20260605/live_neon_qc_benchmark_after_c25_prep.md`: live Neon baseline still FAILS R5 because C25 was not committed
- `python3 tools/canonical_v2_neon_loader.py --dry-run-upsert --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c25_search_quality.json --report data/reports/search_quality_20260605/c25_neon_dry_run_upsert.json --batch-size 500`: PASS, 39,478 rows loaded in transaction, 0 row mapping failures, writes rolled back

## Result

C25 artifact is ready for manual review and approval-gated upload. Remaining publishable unresolved material cases are 4,064 and should stay manual/LLM review unless user approves another costed classification pass.
