# Review Packet: upload-readiness-resume10

stage: UPLOAD-V2
question: Is the D-2 resume10 complete artifact family structurally ready for
user-gated live upload planning into a fresh `canonical_v2_buildings` table?
artifact:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict.resume10_complete.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`

reports:

- `data/reports/canonical_strict_qc.json`
- `data/reports/canonical_v2_upload_dry_run.resume10_complete.json`
- `data/reports/canonical_v2_generic_merge_audit.resume10_complete.json`
- `data/reports/canonical_data_integrity_audit.resume10_complete.json`
- `data/reports/canonical_v2_preupload_qc.resume10_complete.md`

summary:

- strict QC: PASS, 39,776 rows
- upload dry-run: PASS, 39,776 unique PKs, 0 upload-blocking failures
- publishable: 39,736
- nonpublishable: 40
- generic merge audit: PASS, review_required=0
- integrity audit: COMPLETE
- writes so far: none to Neon/R2

known risks:

- 40 rows are intentionally nonpublishable.
- Metadata warnings remain for missing country/city/year/cover fields, but
  these are source-level gaps and are not upload blockers.
- Live upload is still user-gated.
- Recommended path is U3 fresh table, not mutation of the legacy
  metalocus-centric production table.

requested verdict: PASS | WARN | BLOCK
