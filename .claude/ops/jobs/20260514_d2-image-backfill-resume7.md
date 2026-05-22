# Job: d2-image-backfill-resume7

created: 2026-05-14 KST
owner: DB-CODEX-OPS
stage: D-2
status: ready

## Scope

write_scope:
- `data/canonical/country_conflict_refresh/d2_image_backfill_resume7_remaining.json`
- `data/canonical/country_conflict_refresh/d2_image_backfill_resume7_shard*.json`
- `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume7_shard*.jsonl`
- `data/canonical/country_conflict_refresh/d2_failures.image_backfill.resume7_shard*.jsonl`
- `data/canonical/country_conflict_refresh/d2_metrics.image_backfill.resume7_shard*.jsonl`
- `.claude/ops/runs/`

input:
- `data/canonical/country_conflict_refresh/d2_image_backfill_affected.json`
- `data/canonical/country_conflict_refresh/d2_image_backfill_quota_stop_completed.json`
- `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume6_shard*.jsonl`
- `data/canonical/country_conflict_refresh/d2_image_backfill_resume6_image_unavailable.json`
- `data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl`

output:
- remaining D-2 image-derived rows for publishable rows missing `image_derived.style`.

non_goals:
- no upload
- no `upload/` scripts
- no `core/vocab.py` edit
- no reprocessing completed quota-stop/resume6 rows
- no retry for real all-candidate image-unavailable CIDs

## Goal

Finish D-2 after resume6 stopped on Codex usage limit. Resume7 excludes:
- quota-stop completed rows: 7,979
- resume6 completed rows: 1,769
- real image-unavailable row: 1 (`bld_000171`)

## Current State

- total affected rows: 23,008
- cumulative completed before resume7: 9,748
- real image-unavailable excluded: 1
- remaining for resume7: 13,259
- shard counts: 3,315 / 3,315 / 3,315 / 3,314
- Codex exec check: PASS after user bought extra credits
- disk check: about 4.5GiB free before launch

## Smoke Ladder

Same runner/prompt/model path as resume5/resume6.

### N=10

- source: `20260513_214053-d2-image-backfill-resume5.md`
- result: PASS, 10/10 written after cover-fallback fix
- tokens: 38,511 total, about 3,851 tokens/cid
- quality: WARN only for cover-choice risk; schema and controlled vocab passed

### N=100

- source: `20260513_214053-d2-image-backfill-resume5.md`
- result: PASS, 100/100 written
- tokens: 373,782 total, about 3,738 tokens/cid

### Resume6 Actual

- completed before usage stop: 1,769 rows
- metrics tokens: ~6.39M total
- approximate tokens/cid: 6.39M / 1,769 ~= 3,613
- real image-unavailable failures: 1
- parse/vocab failures: 0

### Resume7 Projection

- 13,259 cids * ~3,738 tokens/cid ~= 49.6M tokens
- projected weekly burn: 49.6M / 2.0B ~= 2.5%
- expected codex exec calls: ~1,326 at batch size 10

## Required Command Shape

```bash
python3 tools/canonical_v2_local_enrich.py d2 \
  --affected data/canonical/country_conflict_refresh/d2_image_backfill_resume7_shard<N>.json \
  --e1 data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl \
  --output data/canonical/country_conflict_refresh/d2_results.image_backfill.resume7_shard<N>.jsonl \
  --failures data/canonical/country_conflict_refresh/d2_failures.image_backfill.resume7_shard<N>.jsonl \
  --metrics data/canonical/country_conflict_refresh/d2_metrics.image_backfill.resume7_shard<N>.jsonl \
  --batch-size 10 \
  --model gpt-5.5 \
  --reasoning low \
  --service-tier fast \
  --timeout 600 \
  --ops-job-card .claude/ops/jobs/20260514_d2-image-backfill-resume7.md
```

## Monitoring

- Check written rows/failures/tokens at about 1,000-row intervals.
- Stop on usage-limit, parse/vocab validation failure, or failure rate >1%.
- Keep real image-unavailable failures separate from model/schema failures.

## Abort Conditions

- Codex/API reports credit/quota exhaustion.
- Any process writes outside write_scope.
- `upload/` touched or executed.
- `core/vocab.py` touched.
- `data/id_registry_*.json` touched.
- More than 1% real image-unavailable failures in a 1,000-row interval.
- Validator/schema failure in D-2 output rows.
