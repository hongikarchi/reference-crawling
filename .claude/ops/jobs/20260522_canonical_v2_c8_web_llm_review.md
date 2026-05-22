# canonical_v2 C8 web/LLM review

- status: in_progress
- date: 2026-05-22 KST
- scope: web/LLM review of 72 C7 manual-review candidates; local artifacts only until Neon approval
- input: `data/reports/canonical_v2_backfill_candidates_review_needed.completeness_c7.json`
- review_json: `data/reports/canonical_v2_c8_web_llm_review.json`
- review_md: `data/reports/canonical_v2_c8_web_llm_review.md`

## Review summary

- reviewed_items: 72
- safe_rows_with_updates: 27
- manual_or_no_update_rows: 45
- field_update_counts: location_country=8, location_city=18, project_year=8

## Policy

- Apply only when source gives explicit city/locality/year or local `location_full` can be safely split.
- Country-only source does not create `location_city`.
- Historical/person/future-planning years are not used as `project_year` unless source frames them as the project milestone.
- No Neon/R2/upload mutation in this job before explicit approval.

## Local apply

- status: PASS
- apply_report: `data/reports/canonical_v2_completeness_c8_apply_report.json`
- strict_output: `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c8.json`
- embedded_output: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8.json`
- affected_output: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8_affected.json`
- affected_cids: `data/canonical/country_conflict_refresh/completeness_c8_affected_cids.json`
- affected_cid_count: 27
- applied field updates: location_country=8, location_city=18, project_year=8
- writes: local artifact files only; no Neon/R2/upload mutation

## Validation

- strict_qc: PASS, rows=39,776
- upload_validator: PASS, rows=39,776, unique_pk=39,776
- upload warnings after C8: missing_location_country=1,936, missing_location_city=1,968, missing_project_year=1,291, nonpublishable_rows=40, missing_display_cover_url=39
- gap_inventory: PASS, high_confidence_candidates=0, review_needed_items=48
- review_needed reasons after C8: location_full_present_but_structured_field_missing=27, description_year_candidate_requires_review=21

## Final local pin

- release_manifest: `data/reports/canonical_v2_release_manifest.completeness_c8.json`
- final_quality_report: `data/reports/canonical_v2_final_quality_report.completeness_c8.md`
- neon_status: pending explicit approval for C8 affected-row upsert

## Neon affected-row upsert

- status: PASS
- input: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8_affected_loader.json`
- source_affected: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8_affected.json`
- report: `data/reports/canonical_v2_neon_loader_upsert_completeness_c8_affected.json`
- mode: upsert
- rows_loaded_in_transaction: 27
- row_mapping_failures: 0
- db_counts_seen_in_transaction: total_rows=39,776, unique_pk=39,776, publishable_rows=39,736, nonpublishable_rows=40, missing_embedding=0, missing_display_cover_url=39, needs_image_derived_backfill=0
- writes: committed
- note: loader wrapper file contains the same 27 affected rows under top-level `buildings` for loader compatibility.
