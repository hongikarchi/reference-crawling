# Job: canonical-v2-c6-narrow-apply-prep

created: 2026-05-22 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C6.4-PREP
status: complete

## Scope

write_scope:

- `data/reports/canonical_v2_c6_narrow_apply_prep.json`
- `data/reports/canonical_v2_c6_narrow_apply_prep.md`
- `.claude/ops/jobs/`
- `.claude/Task.md`
- `.claude/REPORT.md`

inputs:

- `data/reports/canonical_v2_c6_source_ranked_apply_queue.json`

## Goal

Prepare the next narrow mutation batch while respecting live-write gates.

## Cost Arithmetic

Local planning only.

```
1 direct local candidate + 37 review-gated web candidates
= 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only apply-prep report only.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No source DB writes.
- No `upload/` edits or execution.

## Result

- direct apply candidate prepared:
  - `bld_038824.project_year = 1989`
- web-derived C6 candidates blocked from mutation until exact-source evidence is
  captured: 34
- actual canonical/Neon mutation remains user-gated.
