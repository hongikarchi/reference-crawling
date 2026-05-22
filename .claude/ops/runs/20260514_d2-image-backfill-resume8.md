# Run: d2-image-backfill-resume8

created: 2026-05-14 KST
stage: D-2
status: stopped-usage-limit
job: `.claude/ops/jobs/20260514_d2-image-backfill-resume8.md`
input: `data/canonical/country_conflict_refresh/d2_image_backfill_resume8_remaining.json`
output: `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume8_lane1.jsonl`

## Launch

- total pending: 12,889
- lane count: 1
- model: `gpt-5.5`
- reasoning: `low`
- service tier: `fast`
- batch size: 10

## Monitor

- latest count: 3,680 / 12,889 rows written
- latest failures: 10 usage-limit rows, 0 image-unavailable, 0 other
- latest tokens: ~14.06M from metrics
- latest retry: 2 recovered attempts, one vocab retry and one parse retry
- next check: blocked until Codex usage limit is available again

## Completion

- exit status: lane process exited code 1 after Codex exec reported usage limit
- output count: resume8 completed 3,680 rows; cumulative D-2 completed 13,798 rows
- structural validation: resume8 output JSON parsed, bad_json=0
- blocked reason: Codex exec raw event says `You've hit your usage limit... try again at May 15th, 2026 9:24 PM`
- partial outputs: `d2_results.patched.resume8_partial.jsonl`, `d2_image_backfill_resume8_partial_completed.json`
- retry manifest: `d2_image_backfill_resume8_usage_limit_retry.json`
- handoff: ENRICH-NEEDS-CLARIFICATION d2_resume8 blocked usage_limit rows=3680 cumulative_d2=13798 retry_usage_limit=10
