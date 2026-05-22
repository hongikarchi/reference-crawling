# Job: canonical-v2-c6-candidate-queue

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C6.1
status: complete

## Scope

write_scope: `tools/canonical_v2_c6_candidate_queue.py`,
`data/reports/canonical_v2_c6_candidate_queue.*`,
`data/reports/canonical_v2_c6_n100_smoke_queue.*`,
`.claude/ops/jobs/`, `.claude/Task.md`, `.claude/REPORT.md`

inputs:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json`
- `data/reports/canonical_v2_c5_local_candidate_verdict.json`
- `data/reports/canonical_v2_c6_web_search_smoke.json`

outputs:

- `data/reports/canonical_v2_c6_candidate_queue.json`
- `data/reports/canonical_v2_c6_candidate_queue.md`
- `data/reports/canonical_v2_c6_n100_smoke_queue.json`
- `data/reports/canonical_v2_c6_n100_smoke_queue.md`

## Goal

Build a durable queue for all remaining C6-relevant missing metadata so the
remaining buildings can be processed systematically rather than by ad hoc
searches.

The queue should classify rows into:

- local apply ready;
- seeded web-apply review from the N=10 smoke;
- web-search location/year candidates;
- policy-null/design-object review candidates;
- conflict/manual candidates.

## Cost Arithmetic

No new web batch and no LLM/API batch. Local JSON classification only.

```
39,776 rows x local queue builder
= 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only queue/report generation only.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No source DB writes.
- No `upload/` edits or execution.

## Result

completed: 2026-05-18 KST

- status: PASS
- rows with any remaining C6 field gap: 2,163
- recommended next-step counts:
  - `c5_local_apply_ready`: 1
  - `c6_seed_apply_review`: 3
  - `policy_null_review`: 177
  - `web_search_location`: 769
  - `web_search_location_year`: 1,065
  - `web_search_year`: 148
- N=100 smoke queue generated: 100 rows
- reports:
  - `data/reports/canonical_v2_c6_candidate_queue.md`
  - `data/reports/canonical_v2_c6_n100_smoke_queue.md`
