# Run: d2-image-backfill-resume10

created: 2026-05-16 KST
stage: D-2
status: complete
job: `.claude/ops/jobs/20260516_d2-image-backfill-resume10.md`
input: `data/canonical/country_conflict_refresh/d2_image_backfill_resume10_remaining.json`
output: `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume10_lane1.jsonl`

## Launch

- total pending: 6,864
- lane count: 1
- model: `gpt-5.5`
- reasoning: `low`
- service tier: `fast`
- batch size: 10
- guard: abort on transient D-2 download failures

## Monitor

- 2026-05-16: initial smoke count 20, failures 0, tokens 88,436.
- 2026-05-16: live count 30, failures 0; runner still active.
- 2026-05-16: live count 80, failures 0, tokens 341,853; runner still active.
- 2026-05-16: guard code committed as `c85ad5a`; tests `python3 -m pytest tests/test_*.py -q` PASS (83).
- 2026-05-16: live count 150, failures 0, tokens 633,874; output JSONL schema smoke PASS.
- 2026-05-16: live count 210, failures 0, tokens 887,986.
- 2026-05-17: user approved speed over small token overhead until weekly remaining 50%.
- 2026-05-17: stopped single lane after 270 rows, failures 0, tokens 1,143,355.
- 2026-05-17: split remaining 6,594 rows into parallel lane2=1,649, lane3=1,649, lane4=1,648, lane5=1,648.
- 2026-05-17: sandboxed parallel attempts stopped on transient DNS before any writes; not counted as completed.
- 2026-05-17: restarted 4 lanes with network escalation due sandbox DNS failures.
- 2026-05-17: net-lane smoke PASS; total output 350, failures 0, duplicate_cids 0.
- 2026-05-17: net-lane live total 510, failures 0.
- 2026-05-17: net-lane 10m monitor total 880, failures 0, duplicate_cids 0.
- 2026-05-17: net-lane 15m monitor total 1,630, failures 0, duplicate_cids 0.
- 2026-05-17: net-lane 30m monitor total 2,420, failures 0, duplicate_cids 0.
- 2026-05-17: net-lane 45m monitor total 3,170, failures 0, duplicate_cids 0.
- 2026-05-17: net-lane 60m monitor total 3,960, failures 0, duplicate_cids 0.
- 2026-05-17: net-lane 75m monitor total 4,980, failures 0, duplicate_cids 0.
- 2026-05-17: net-lane 90m monitor total 6,010, failures 0, duplicate_cids 0.
- 2026-05-17: net-lane 100m monitor total 6,750, failures 0, duplicate_cids 0.
- 2026-05-17: resume10 complete 6,864/6,864, failures 0, duplicate_cids 0, tokens 30,809,514.
- 2026-05-17: strict rebuild PASS; `needs_image_derived_backfill=0`, publishable 39,736, nonpublishable 40.
- 2026-05-17: strict QC PASS, upload validator PASS, generic merge audit PASS, integrity audit COMPLETE.
- 2026-05-17: legacy `canonical.reviewer_gate --stage F` is incompatible with strict_v2 schema; ignored in favor of `qc_strict.py`.
- next check: none; resume10 complete.

## Completion

- exit status: success
- output count: resume10 6,864 rows; final D-2 patched JSONL 39,776 rows
- structural validation: PASS
- strict artifact: `data/canonical/country_conflict_refresh/canonical_buildings_strict.resume10_complete.json`
- embedded artifact: `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`
- reports:
  - `data/reports/canonical_strict_qc.json`
  - `data/reports/canonical_v2_upload_dry_run.resume10_complete.json`
  - `data/reports/canonical_v2_generic_merge_audit.resume10_complete.json`
  - `data/reports/canonical_data_integrity_audit.resume10_complete.json`
- handoff:
