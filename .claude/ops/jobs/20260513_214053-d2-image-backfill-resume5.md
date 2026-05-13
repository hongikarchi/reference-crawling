# Job: d2-image-backfill-resume5

created: 2026-05-13 21:40:53 KST
owner: ENRICHER
stage: D-2
status: ready-for-db-codex-ops

## Scope

write_scope:
- `data/canonical/country_conflict_refresh/d2_results.image_backfill.full_resume5_shard*.jsonl`
- `data/canonical/country_conflict_refresh/d2_failures.image_backfill.full_resume5_shard*.jsonl`
- `data/canonical/country_conflict_refresh/d2_metrics.image_backfill.full_resume5_shard*.jsonl`
- `.claude/ops/runs/`

input:
- `data/canonical/country_conflict_refresh/d2_image_backfill_affected.json`
- `data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl`

output:
- D-2 image-derived rows for publishable rows with missing `image_derived.style`.

non_goals:
- no upload
- no `upload/` scripts
- no `core/vocab.py` edit
- no legacy `tools/dispatch.sh` / `tools/poll.sh`
- no legacy `DB-CRAWLER`, `DB-MATCHER`, `DB-ENRICHER` orchestration

## Goal

Resume D-2 image analysis using DB Ops Mode only. This job exists because
the previous direct single-chat launch violated the agreed workflow. All
large D-2 runs must pass `--ops-job-card` and be launched from
`DB-CODEX-OPS` / DB Ops process lanes, not from an ad hoc chat turn.

## Current State

- affected publishable rows needing D-2 backfill: 23,008
- completed rows before this job: 2,909
  - seed smoke/full carryover: 100
  - prior full shards: 2,769
  - interrupted resume5 shards: 40
- known image-unavailable CID: `bld_000171`
- remaining expected before resume: about 20,098 rows, minus any rows written
  after this card was created.

## Smoke Ladder

### N=10

- command: `python3 tools/canonical_v2_local_enrich.py d2 ... --limit 10 --batch-size 10 --max-batches 1`
- result: PASS, 10/10 written after cover-fallback fix
- tokens: 38,511 total, about 3,851 tokens/cid
- quality: WARN; sample exposed non-exterior/drawing cover risk, but schema and
  controlled vocab passed

### N=100

- command: N=10 seed + 90 extra rows, batch size 10
- result: PASS, 100/100 written
- tokens: 373,782 total, about 3,738 tokens/cid
- projected full: 23,008 * 3,738 ~= 86.0M tokens
- projected weekly burn: 86.0M / 2.0B ~= 4.3%
- quality: WARN; image-derived schema valid, but downstream bad-cover QC is
  still required before final publishability promotion

### Full

approval:
- user explicitly approved proceeding down to weekly 5% remaining on
  2026-05-13.

required command shape:

```bash
python3 tools/canonical_v2_local_enrich.py d2 \
  --affected data/canonical/country_conflict_refresh/d2_image_backfill_resume5_shard<N>.json \
  --e1 data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl \
  --output data/canonical/country_conflict_refresh/d2_results.image_backfill.full_resume5_shard<N>.jsonl \
  --failures data/canonical/country_conflict_refresh/d2_failures.image_backfill.full_resume5_shard<N>.jsonl \
  --metrics data/canonical/country_conflict_refresh/d2_metrics.image_backfill.full_resume5_shard<N>.jsonl \
  --batch-size 10 \
  --model gpt-5.5 \
  --reasoning low \
  --service-tier fast \
  --timeout 600 \
  --ops-job-card .claude/ops/jobs/20260513_214053-d2-image-backfill-resume5.md
```

## Monitoring

- Check progress every 1,000 newly written rows.
- Run `./tools/quota_check.sh`; continue only while weekly remaining is above
  5%.
- If `canonical_v2_local_enrich.py` returns nonzero, inspect failure file.
  - all-candidate image 404: keep that CID in failures and continue with
    remaining rows
  - vocab/parse failure: retry once is already built into the runner
  - repeated parse/vocab failure after retry: stop and inspect sample

## Abort Conditions

- weekly remaining <= 5%
- 5h remaining <= 5%
- any process writes outside write_scope
- `upload/` touched or executed
- `core/vocab.py` touched
- `data/id_registry_*.json` touched
- more than 1% real image-unavailable failures in a 1,000-row interval
- validator/schema failure in D-2 output rows
- operator attempts legacy team dispatch instead of DB Ops

## Resume Notes

The runner now requires `--ops-job-card` for non-dry-run jobs with more than
100 pending CIDs. This is a mechanical guardrail to prevent another direct
full/shard launch from an ad hoc chat turn.
