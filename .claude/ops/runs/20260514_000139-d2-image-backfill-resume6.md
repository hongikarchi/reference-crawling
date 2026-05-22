# Run: d2-image-backfill-resume6

created: 2026-05-14 00:01:39 KST
stage: D-2
status: stopped-usage-limit-and-disk-full
job: `.claude/ops/jobs/20260514_000139-d2-image-backfill-resume6.md`
input: `data/canonical/country_conflict_refresh/d2_image_backfill_resume6_shard{1,2,3,4}.json`
output: `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume6_shard{1,2,3,4}.jsonl`

## Launch

- shard counts: 3,758 / 3,757 / 3,757 / 3,757
- total pending: 15,029
- model: `gpt-5.5`
- reasoning: `low`
- service tier: `fast`
- batch size: 10

## Monitor

- latest count: 1,769 / 15,029 rows written at 2026-05-14 ~00:39 KST
- latest failures: 1 real image 404 (`bld_000171`) + 40 usage-limit batch rows to retry
- latest tokens: ~6.39M total from metrics
- next check: blocked until Codex usage limit / credits are available and disk space is freed

## Completion

- exit status: shard processes exited code 1 after Codex exec reported usage limit
- output count: resume6 completed 1,769 rows; cumulative D-2 completed 9,748 rows
- structural validation: resume6 output JSON parsed, bad_json=0; full strict partial build succeeded before publishability override
- blocked reason: Codex exec raw event says `You've hit your usage limit... try again at May 15th, 2026 9:24 PM`
- disk reason: filesystem had only 583MB free; writing `canonical_buildings_strict.resume6_partial.publishability.json` failed with ENOSPC
- partial outputs: `d2_results.patched.resume6_partial.jsonl`, `canonical_buildings_strict.resume6_partial.json`
- retry manifest: `d2_image_backfill_resume6_usage_limit_retry.json`
- real image-unavailable manifest: `d2_image_backfill_resume6_image_unavailable.json`
- handoff: ENRICH-NEEDS-CLARIFICATION d2_resume6 blocked usage_limit_and_disk rows=1769 retry_usage_limit=40 image_unavailable=1
