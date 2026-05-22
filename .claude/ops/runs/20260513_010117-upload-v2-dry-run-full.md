# Run: upload-v2-dry-run-full

created: 2026-05-13 01:01:17
stage: UPLOAD-V2
status: done
pid: pending
log: local-terminal
output: data/reports/canonical_v2_upload_dry_run_full.json

## Command

```bash
python3 tools/canonical_v2_upload_validator.py --report data/reports/canonical_v2_upload_dry_run_full.json
```

## Monitor

- latest count: 39,759 rows validated
- latest error: none
- ETA: complete
- next check: blocked by generic merge audit, not upload

## Completion

- exit status: 0
- output count: 39,759 rows, 39,759 unique PKs
- structural validation: PASS, no upload-blocking failures
- handoff: continue to generic merge repair before any live upload
