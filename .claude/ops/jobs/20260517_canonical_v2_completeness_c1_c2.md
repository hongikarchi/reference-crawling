# Job: canonical-v2-completeness-c1-c2

created: 2026-05-17 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C1-C2
status: complete

## Scope

write_scope: `tools/canonical_v2_gap_inventory.py`, `data/reports/`,
`.claude/ops/jobs/`, `.claude/Task.md`, `.claude/REPORT.md`

input:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`
- source DBs under `data/crawl/`
- `data/canonical/architects_canonical.json`

outputs:

- `data/reports/canonical_v2_gap_inventory.json`
- `data/reports/canonical_v2_gap_inventory.md`
- `data/reports/canonical_v2_backfill_candidates_high_confidence.json`
- `data/reports/canonical_v2_backfill_candidates_review_needed.json`
- `data/reports/canonical_v2_manual_review_queue.md`

## Goal

Complete C1/C2 as read-only analysis:

- C1: inventory missing canonical fields and classify recovery source.
- C2: emit deterministic high-confidence backfill candidates separately from
  review-needed candidates.

## Cost Arithmetic

No LLM batch work launched.

```
0 cids x (~0 prompt tokens + ~0 output tokens + ~0 codex batch overhead)
= 0 pipeline tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- No canonical artifact mutation.
- No source DB mutation.
- No Neon/R2 write.
- No upload code modification or execution.
- High-confidence means structured source DB values agree on one value or a
  deterministic registry/source-URL mapping exists.
- Text extraction and source conflicts stay in review queue.

## Run Log

- Attempt 1 failed before analysis with `ModuleNotFoundError: No module named
  'tools'` when executing by file path. Fix: insert repo root into `sys.path`
  before importing local modules.
- Attempt 2 PASS:
  - rows inspected: 39,776
  - high-confidence deterministic candidates: 0
  - review-needed items: 217
  - review reasons:
    - `description_year_candidate_requires_review`: 136
    - `location_full_present_but_structured_field_missing`: 81
  - writes: reports only

## Completion

C1/C2 are complete as read-only analysis.

Reports:

- `data/reports/canonical_v2_gap_inventory.json`
- `data/reports/canonical_v2_gap_inventory.md`
- `data/reports/canonical_v2_backfill_candidates_high_confidence.json`
- `data/reports/canonical_v2_backfill_candidates_review_needed.json`
- `data/reports/canonical_v2_manual_review_queue.md`

Conclusion: under strict deterministic rules, there are no safe automatic
backfills left. Remaining candidates require review/policy because they come
from source descriptions or raw `location_full` strings, not structured source
fields.
