# Job: canonical-v2-c6-source-ranked-apply-queue

created: 2026-05-22 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C6.3
status: complete

## Scope

write_scope:

- `data/reports/canonical_v2_c6_source_ranked_apply_queue.json`
- `data/reports/canonical_v2_c6_source_ranked_apply_queue.md`
- `.claude/ops/jobs/`
- `.claude/Task.md`
- `.claude/REPORT.md`

inputs:

- `data/reports/canonical_v2_c5_local_candidate_verdict.json`
- `data/reports/canonical_v2_c6_web_search_smoke.json`
- `data/reports/canonical_v2_c6_n100_web_search_smoke.json`

## Goal

Create the mutation boundary for C6: a ranked queue of candidates that can be
reviewed for exact-source identity before any canonical/Neon apply.

## Cost Arithmetic

Local report generation from existing C5/C6 smoke results only.

```
existing reports x local queue synthesis
= 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only report generation only.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No source DB writes.
- No `upload/` edits or execution.

## Result

- local apply-ready candidates: 1
- C6 seed candidates from N=10 smoke: 3
- C6 N=100 apply-after-exact-source candidates: 34
- combined mutation-candidate pool with seed overlap: 38
- unique mutation-candidate pool: 35
- source-ranked queue status: review_required_before_apply
- report:
  `data/reports/canonical_v2_c6_source_ranked_apply_queue.md`

Conclusion: next mutation step must be C6.4 narrow apply after exact-source
review, not bulk web-search apply.
