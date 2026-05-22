# Run: d2-image-backfill-resume7

created: 2026-05-14 KST
stage: D-2
status: stopped-usage-limit
job: `.claude/ops/jobs/20260514_d2-image-backfill-resume7.md`
input: `data/canonical/country_conflict_refresh/d2_image_backfill_resume7_shard{1,2,3,4}.json`
output: `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume7_shard{1,2,3,4}.jsonl`

## Launch

- shard counts: 3,315 / 3,315 / 3,315 / 3,314
- total pending: 13,259
- model: `gpt-5.5`
- reasoning: `low`
- service tier: `fast`
- batch size: 10

## Monitor

- latest count: 370 / 13,259 rows written before stop
- latest failures: 40 usage-limit rows, 0 image-unavailable, 0 other
- latest tokens: ~1.34M total from metrics
- next check: blocked until Codex usage limit/credits allow sustained image exec

## Completion

- exit status: shard processes exited code 1 after Codex exec reported usage limit
- output count: resume7 completed 370 rows; cumulative D-2 completed 10,118 rows
- structural validation: resume7 output JSON parsed, bad_json=0
- blocked reason: Codex exec raw event says `You've hit your usage limit... try again at May 15th, 2026 9:24 PM`
- partial outputs: `d2_results.patched.resume7_partial.jsonl`, `d2_image_backfill_resume7_partial_completed.json`
- retry manifest: `d2_image_backfill_resume7_usage_limit_retry.json`
- handoff: ENRICH-NEEDS-CLARIFICATION d2_resume7 blocked usage_limit rows=370 cumulative_d2=10118 retry_usage_limit=40
