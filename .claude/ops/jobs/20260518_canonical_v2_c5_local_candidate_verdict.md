# Job: canonical-v2-c5-local-candidate-verdict

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C5.1
status: complete

## Scope

write_scope: `tools/canonical_v2_c5_local_candidate_verdict.py`,
`data/reports/canonical_v2_c5_local_candidate_verdict.*`,
`.claude/ops/jobs/`, `.claude/Task.md`, `.claude/REPORT.md`

input:

- `data/reports/canonical_v2_crawler_gap_audit.json`

outputs:

- `data/reports/canonical_v2_c5_local_candidate_verdict.json`
- `data/reports/canonical_v2_c5_local_candidate_verdict.md`

## Goal

Classify the 9 high-value local candidates from C5:

- 2 raw-location candidates;
- 7 year text completion-signal candidates.

Determine which, if any, are safe enough for a later narrow canonical apply.

## Cost Arithmetic

No batch LLM/API work and no network.

```
9 candidates x inline Codex semantic judgment
= negligible session tokens, 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only verdict reports only.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No source DB writes.
- No `upload/` edits or execution.

## Result

completed: 2026-05-18 KST

- verdict status: PASS
- total candidates: 9
- safe apply candidates: 1
- keep-null candidates: 8
- apply candidate:
  - `bld_038824.project_year = 1989`
- reports:
  - `data/reports/canonical_v2_c5_local_candidate_verdict.json`
  - `data/reports/canonical_v2_c5_local_candidate_verdict.md`
