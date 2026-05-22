# canonical_v2 C7 local apply

- status: complete
- date: 2026-05-22 KST
- scope: apply C6.5 exact-source safe updates to local canonical artifacts only
- neon_write: not run
- upload_write: not run

## Inputs

- strict: `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c6.json`
- embedded: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c6.json`
- review: `data/reports/canonical_v2_c6_exact_source_review.json`

## Outputs

- strict: `data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c7.json`
- embedded: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c7.json`
- affected: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c7_affected.json`
- affected_cids: `data/canonical/country_conflict_refresh/completeness_c7_affected_cids.json`
- apply_report: `data/reports/canonical_v2_completeness_c7_apply_report.json`
- strict_qc: `data/reports/canonical_strict_qc.completeness_c7.json`
- upload_validator: `data/reports/canonical_v2_upload_dry_run.completeness_c7.json`
- gap_inventory: `data/reports/canonical_v2_gap_inventory.completeness_c7.json`

## Apply result

- status: PASS
- planned_update_cids: 33
- affected_cid_count: 33
- location_country_updates: 29
- location_city_updates: 26
- project_year_updates: 4
- project_year_skipped_same_value: 12
- project_year_skipped_not_empty: 2

## Validation

- strict_qc: PASS, rows=39,776
- upload_validator: PASS, rows=39,776, unique_pk=39,776, missing_project_year=1,299, missing_location_country=1,938, missing_location_city=1,986
- gap_inventory: PASS, high_confidence_candidates=0, review_needed_items=72
- review_needed reasons: location_full_present_but_structured_field_missing=43, description_year_candidate_requires_review=29

## Next gate

Neon affected-row upsert for C7 is pending explicit user approval.

## Neon affected-row upsert

- status: PASS
- input: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c7_affected.json`
- report: `data/reports/canonical_v2_neon_loader_upsert_completeness_c7_affected.json`
- mode: upsert
- rows_loaded_in_transaction: 33
- row_mapping_failures: 0
- db_counts_seen_in_transaction: total_rows=39,776, unique_pk=39,776, publishable_rows=39,736, nonpublishable_rows=40, missing_embedding=0, missing_display_cover_url=39, needs_image_derived_backfill=0
- writes: committed
