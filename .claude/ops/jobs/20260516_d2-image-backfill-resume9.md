# Job: d2-image-backfill-resume9

created: 2026-05-16 KST
owner: DB-CODEX-OPS
stage: D-2
status: ready

## Scope

write_scope:
- `data/canonical/country_conflict_refresh/d2_image_backfill_resume9_remaining.json`
- `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume9_lane1.jsonl`
- `data/canonical/country_conflict_refresh/d2_failures.image_backfill.resume9_lane1.jsonl`
- `data/canonical/country_conflict_refresh/d2_metrics.image_backfill.resume9_lane1.jsonl`
- `.claude/ops/runs/`

input:
- `data/canonical/country_conflict_refresh/d2_image_backfill_affected.json`
- all completed D-2 outputs through resume8
- `data/canonical/country_conflict_refresh/d2_image_backfill_resume6_image_unavailable.json`
- `data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl`

output:
- remaining D-2 image-derived rows for publishable rows missing `image_derived.style`.

non_goals:
- no upload
- no `upload/` scripts
- no `core/vocab.py` edit
- no reprocessing completed rows
- no retry for real all-candidate image-unavailable CIDs

## Goal

Continue D-2 after resume8 stopped on Codex usage limit.

## Current State

- total affected rows: 23,008
- cumulative completed before resume9: 13,798
- real image-unavailable excluded: 1
- remaining for resume9: 9,209
- launch mode: 1 lane
- model: `gpt-5.5`
- reasoning: `low`
- service tier: `fast`

## Smoke Ladder

Same runner/prompt/model path as resume5/resume6/resume7/resume8.

### N=10

- source: `20260513_214053-d2-image-backfill-resume5.md`
- result: PASS, 10/10 written after cover-fallback fix
- tokens: 38,511 total, about 3,851 tokens/cid

### N=100

- source: `20260513_214053-d2-image-backfill-resume5.md`
- result: PASS, 100/100 written
- tokens: 373,782 total, about 3,738 tokens/cid

### Resume8 Actual

- completed: 3,680 rows
- failures: 10 usage-limit rows, 0 image-unavailable, 0 other
- final output bad_json: 0
- single-lane mode limited usage-limit waste to one 10-row batch.

### Resume9 Projection

- 9,209 cids * ~3,738 tokens/cid ~= 34.4M tokens
- expected codex exec calls: ~921 at batch size 10
- continue single-lane mode to minimize usage-limit retry waste.

## Required Command Shape

```bash
python3 tools/canonical_v2_local_enrich.py d2 \
  --affected data/canonical/country_conflict_refresh/d2_image_backfill_resume9_remaining.json \
  --e1 data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl \
  --output data/canonical/country_conflict_refresh/d2_results.image_backfill.resume9_lane1.jsonl \
  --failures data/canonical/country_conflict_refresh/d2_failures.image_backfill.resume9_lane1.jsonl \
  --metrics data/canonical/country_conflict_refresh/d2_metrics.image_backfill.resume9_lane1.jsonl \
  --batch-size 10 \
  --model gpt-5.5 \
  --reasoning low \
  --service-tier fast \
  --timeout 600 \
  --ops-job-card .claude/ops/jobs/20260516_d2-image-backfill-resume9.md
```

## Monitoring

- Check written rows/failures/tokens at short intervals first, then about
  1,000-row intervals.
- Stop on usage-limit, parse/vocab validation failure that is not recovered by
  built-in retry, or failure rate >1%.

## Abort Conditions

- Codex/API reports credit/quota exhaustion.
- Any process writes outside write_scope.
- `upload/` touched or executed.
- `core/vocab.py` touched.
- `data/id_registry_*.json` touched.
- More than 1% real image-unavailable failures in a 1,000-row interval.
- Validator/schema failure in D-2 output rows.
