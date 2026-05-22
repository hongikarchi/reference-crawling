# Job: canonical-v2-c6-exact-source-review

created: 2026-05-22 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C6.5
status: complete

## Scope

write_scope:

- `data/reports/canonical_v2_c6_exact_source_review.json`
- `data/reports/canonical_v2_c6_exact_source_review.md`
- `.claude/ops/jobs/`
- `.claude/Task.md`
- `.claude/REPORT.md`

inputs:

- `data/reports/canonical_v2_c6_source_ranked_apply_queue.json`
- targeted web/source evidence gathered during C6.2/C6.5

## Goal

Review the 34 unique web-derived C6 candidates using exact project/source
identity, then separate safe field updates from manual/null decisions.

## Cost Arithmetic

Targeted web/source review only. No batch LLM/API pipeline.

```
34 candidates x exact-source web review
= session interaction only, 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only review report only.
- No canonical artifact mutation in C6.5.
- No Neon/R2 writes.
- No source DB writes.
- No `upload/` edits or execution.

## Result

- reviewed candidates: 34
- safe rows with at least one field update: 33
- manual-only rows: 1
- safe field updates: 73
- project-year updates: 18
- location-country updates: 29
- location-city updates: 26
- report:
  `data/reports/canonical_v2_c6_exact_source_review.md`
