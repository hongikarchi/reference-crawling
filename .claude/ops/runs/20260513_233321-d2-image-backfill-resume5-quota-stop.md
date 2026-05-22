# Run: d2-image-backfill-resume5-quota-stop

created: 2026-05-13 23:33:21
stage: D-2
status: stopped-quota-abort
pid: 50818,50861,50853,50858
log: logs/d2_image_backfill_resume5_shard{1,2,3,4}_20260513_2158.log
output: data/canonical/country_conflict_refresh/d2_results.image_backfill.full_resume5_shard*.jsonl

## Command

```bash
kill 50818 50861 50853 50858 after ALERT_THRESHOLD=5 ./tools/quota_check.sh reported weekly=5%; active shard commands all used --ops-job-card .claude/ops/jobs/20260513_214053-d2-image-backfill-resume5.md
```

## Monitor

- latest count:
- latest error:
- ETA:
- next check:

## Completion

- exit status: stopped intentionally at weekly quota 5%; shard PIDs no longer running
- output count: D-2 completed 7,979 / 23,008 affected; remaining 15,029
- structural validation: PASS hard checks; strict QC WARN only for incomplete image_derived backfill
- postprocess outputs: `d2_results.image_backfill.quota_stop_deduped.jsonl`, `d2_results.patched.quota_stop.jsonl`, `canonical_buildings_strict.quota_stop.json`, `canonical_buildings_strict_embedded.quota_stop.json`
- upload validator: PASS (`data/reports/canonical_v2_upload_dry_run_quota_stop.json`)
- integrity audit: COMPLETE (`data/reports/canonical_data_integrity_audit_quota_stop.json`)
- publishability check: 39 image-less rows are nonpublishable; publishable rows missing image/cover = 0
- handoff: ENRICH-DONE d2_image_backfill_quota_stop_partial rows=7979 remaining=15029 qc=PASS_WARN
