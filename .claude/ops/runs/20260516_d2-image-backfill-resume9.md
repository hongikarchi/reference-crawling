# Run: d2-image-backfill-resume9

created: 2026-05-16 KST
stage: D-2
status: stopped-network-abort
job: `.claude/ops/jobs/20260516_d2-image-backfill-resume9.md`
input: `data/canonical/country_conflict_refresh/d2_image_backfill_resume9_remaining.json`
output: `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume9_lane1.jsonl`

## Launch

- total pending: 9,209
- lane count: 1
- model: `gpt-5.5`
- reasoning: `low`
- service tier: `fast`
- batch size: 10

## Monitor

- latest count: 2,345 / 9,209 rows written
- latest failures: 4,375 transient DNS/network download failures; 0 real image-unavailable; 0 usage-limit
- latest tokens: ~9.35M from metrics
- next check: retry after runner transient-network guard

## Completion

- exit status: operator killed runaway network-failure loop after abort threshold exceeded
- output count: resume9 completed 2,345 rows; cumulative D-2 completed 16,143 rows
- structural validation: resume9 output JSON parsed, bad_json=0
- blocked reason: transient image-host DNS/network failures (`[Errno 8] nodename nor servname provided`)
- important: these failures are retryable and must not be marked nonpublishable
- partial outputs: `d2_results.patched.resume9_partial.jsonl`, `d2_image_backfill_resume9_partial_completed.json`
- retry manifest: `d2_image_backfill_resume9_network_retry.json`
- guard added: `tools/canonical_v2_local_enrich.py` now aborts transient D-2 download failures instead of per-CID skipping
- guard test: `python3 -m pytest tests/test_canonical_v2_local_enrich.py -q` -> 7 passed
- handoff: ENRICH-NEEDS-CLARIFICATION d2_resume9 stopped_network rows=2345 cumulative_d2=16143 retry_network=4375
