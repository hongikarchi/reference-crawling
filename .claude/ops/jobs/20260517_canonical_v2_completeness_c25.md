# Job: canonical-v2-completeness-c2.5

created: 2026-05-17 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C2.5
status: complete

## Scope

write_scope: `tools/canonical_v2_review_rule_candidates.py`,
`data/reports/`, `.claude/ops/jobs/`, `.claude/Task.md`, `.claude/REPORT.md`

inputs:

- `data/reports/canonical_v2_backfill_candidates_review_needed.json`
- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`

outputs:

- `data/reports/canonical_v2_review_rule_candidates.json`
- `data/reports/canonical_v2_review_rule_candidates.md`

## Goal

Classify the 217 C1/C2 review-needed candidates into:

- candidates that are safe only after explicit user policy approval; and
- candidates that still require manual/semantic review.

## Cost Arithmetic

No LLM batch work launched.

```
0 cids x (~0 prompt tokens + ~0 output tokens + ~0 codex batch overhead)
= 0 pipeline tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only reports only.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No `upload/` edits or execution.
- Description-year extraction remains policy-safe, not automatically trusted.
- `location_full` parsing only accepts conservative `City - Country` or
  `City, Country` patterns.

## Result

Command:

```bash
python3 tools/canonical_v2_review_rule_candidates.py
```

Result:

- total review items: 217
- safe after explicit policy approval: 91
- keep review: 126
- safe candidates by field:
  - `project_year`: 91
- keep-review reasons:
  - `single_location_string`: 76
  - `multiple_description_year_candidates`: 35
  - `future_description_year_candidate`: 10
  - `parsed_city_is_known_country`: 4
  - `parsed_country_conflicts_with_canonical_country`: 1
- writes: reports only

Reports:

- `data/reports/canonical_v2_review_rule_candidates.json`
- `data/reports/canonical_v2_review_rule_candidates.md`

Conclusion: no `location_city` candidates pass the conservative parser. The
only policy-safe candidates are 91 `project_year` rows where source description
has exactly one non-future year.
