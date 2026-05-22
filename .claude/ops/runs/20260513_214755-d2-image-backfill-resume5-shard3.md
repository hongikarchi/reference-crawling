# Run: d2-image-backfill-resume5-shard3

created: 2026-05-13 21:47:55
stage: D-2
status: running
pid: 45739
log: logs/d2_image_backfill_resume5_shard3_20260513_2152.log
output: data/canonical/country_conflict_refresh/d2_results.image_backfill.full_resume5_shard3.jsonl

## Command

```bash
python3 tools/canonical_v2_local_enrich.py d2 --affected data/canonical/country_conflict_refresh/d2_image_backfill_resume5_shard3.json --e1 data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl --output data/canonical/country_conflict_refresh/d2_results.image_backfill.full_resume5_shard3.jsonl --failures data/canonical/country_conflict_refresh/d2_failures.image_backfill.full_resume5_shard3.jsonl --metrics data/canonical/country_conflict_refresh/d2_metrics.image_backfill.full_resume5_shard3.jsonl --batch-size 10 --model gpt-5.5 --reasoning low --service-tier fast --timeout 600 --ops-job-card .claude/ops/jobs/20260513_214053-d2-image-backfill-resume5.md
```

## Monitor

- latest count:
- latest error:
- ETA:
- next check:

## Completion

- exit status:
- output count:
- structural validation:
- handoff:
