# Job: d2-image-backfill-resume6

created: 2026-05-14 00:01:39 KST
owner: DB-CODEX-OPS
stage: D-2
status: ready

## Scope

write_scope:
- `data/canonical/country_conflict_refresh/d2_image_backfill_resume6_remaining.json`
- `data/canonical/country_conflict_refresh/d2_image_backfill_resume6_shard*.json`
- `data/canonical/country_conflict_refresh/d2_results.image_backfill.resume6_shard*.jsonl`
- `data/canonical/country_conflict_refresh/d2_failures.image_backfill.resume6_shard*.jsonl`
- `data/canonical/country_conflict_refresh/d2_metrics.image_backfill.resume6_shard*.jsonl`
- `.claude/ops/runs/`

input:
- `data/canonical/country_conflict_refresh/d2_image_backfill_affected.json`
- `data/canonical/country_conflict_refresh/d2_image_backfill_quota_stop_completed.json`
- `data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl`

output:
- remaining D-2 image-derived rows for publishable rows missing `image_derived.style`.

non_goals:
- no upload
- no `upload/` scripts
- no `core/vocab.py` edit
- no legacy `DB-CRAWLER`, `DB-MATCHER`, `DB-ENRICHER` orchestration
- no reprocessing already completed D-2 quota-stop rows

## Goal

Finish the D-2 image analysis backfill after the 2026-05-13 weekly-quota stop.
Use DB Ops artifacts only. The previous completed manifest is the exclusion
source of truth.

## Current State

- total affected publishable rows needing D-2 backfill: 23,008
- completed before resume6: 7,979
- remaining for resume6: 15,029
- current strict artifact: `canonical_buildings_strict.quota_stop.json`
- current embedded artifact: `canonical_buildings_strict_embedded.quota_stop.json`
- user approval: user bought 1,000 extra credits and approved continuing on
  2026-05-14.
- quota note: `tools/quota_check.sh` requires cmux workspace `/status`; this
  single DB-CODEX-OPS session cannot parse it. Continue with batch metrics and
  stop on Codex/API credit errors, validation failure, or user interruption.

## Smoke Ladder

This is the same runner/prompt/model path as resume5.

### N=10

- source: `20260513_214053-d2-image-backfill-resume5.md`
- result: PASS, 10/10 written after cover-fallback fix
- tokens: 38,511 total, about 3,851 tokens/cid
- quality: WARN only for cover-choice risk; schema and controlled vocab passed

### N=100

- source: `20260513_214053-d2-image-backfill-resume5.md`
- result: PASS, 100/100 written
- tokens: 373,782 total, about 3,738 tokens/cid
- projected resume6: 15,029 * 3,738 ~= 56.2M tokens
- projected weekly burn: 56.2M / 2.0B ~= 2.8%

### Full

Run 4 shards, each with `--ops-job-card` and low/fast model settings.

Required command shape:

```bash
python3 tools/canonical_v2_local_enrich.py d2 \
  --affected data/canonical/country_conflict_refresh/d2_image_backfill_resume6_shard<N>.json \
  --e1 data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl \
  --output data/canonical/country_conflict_refresh/d2_results.image_backfill.resume6_shard<N>.jsonl \
  --failures data/canonical/country_conflict_refresh/d2_failures.image_backfill.resume6_shard<N>.jsonl \
  --metrics data/canonical/country_conflict_refresh/d2_metrics.image_backfill.resume6_shard<N>.jsonl \
  --batch-size 10 \
  --model gpt-5.5 \
  --reasoning low \
  --service-tier fast \
  --timeout 600 \
  --ops-job-card .claude/ops/jobs/20260514_000139-d2-image-backfill-resume6.md
```

## Monitoring

- Check written rows and failures every ~1,000 newly completed rows.
- Validate schema/vocab on accumulated outputs before patching strict artifact.
- If a CID has all image candidates unavailable, log failure and continue.
- If parse/vocab failure survives built-in retry, stop and inspect sample.

## Abort Conditions

- Codex/API reports credit/quota exhaustion.
- Any process writes outside write_scope.
- `upload/` touched or executed.
- `core/vocab.py` touched.
- `data/id_registry_*.json` touched.
- More than 1% real image-unavailable failures in a 1,000-row interval.
- Validator/schema failure in D-2 output rows.
