# Job: canonical-v2-remaining-candidate-verification

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C4-VERIFY
status: complete

## Scope

write_scope: `tools/canonical_v2_remaining_review_verdict.py`,
`data/reports/canonical_v2_remaining_review_verdict.*`,
`.claude/ops/jobs/`, `.claude/Task.md`, `.claude/REPORT.md`

inputs:

- `data/reports/canonical_v2_backfill_candidates_review_needed.completeness_c3.json`
- `data/reports/canonical_v2_llm_location_adjudication.json`

outputs:

- `data/reports/canonical_v2_remaining_review_verdict.json`
- `data/reports/canonical_v2_remaining_review_verdict.md`

## Goal

Verify the 88 candidates left after C3 without applying them. Separate:

- project-year candidates that are strong enough for a future policy-approved
  apply step; and
- candidates that should remain null/manual-review because the evidence is
  historical, biographical, country-only, speculative, or otherwise ambiguous.

## Cost Arithmetic

No batch LLM subprocess launched.

```
88 cids x semantic verification in current approved chat turn
= negligible incremental pipeline tokens
projected weekly burn: <0.01% of 2B
```

## Guardrails

- Read-only reports only.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No `upload/` edits or execution.
- Do not write country-only values into `location_city`.

## Result

Command:

```bash
python3 tools/canonical_v2_remaining_review_verdict.py
```

Result:

- total remaining items: 88
- project-year policy apply candidates: 15
- project-year keep/manual review: 30
- location verified keep city null: 38
- location manual review: 5
- writes: reports only

Reports:

- `data/reports/canonical_v2_remaining_review_verdict.json`
- `data/reports/canonical_v2_remaining_review_verdict.md`

Conclusion: no further `location_city` candidates should be applied
automatically. A future C4 apply step can apply 15 `project_year` updates after
explicit user approval.
