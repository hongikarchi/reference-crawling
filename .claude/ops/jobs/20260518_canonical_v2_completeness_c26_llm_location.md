# Job: canonical-v2-completeness-c2.6-llm-location

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C2.6
status: complete

## Scope

write_scope: `tools/canonical_v2_llm_location_adjudication.py`,
`data/reports/`, `.claude/ops/jobs/`, `.claude/Task.md`, `.claude/REPORT.md`

input:

- `data/reports/canonical_v2_backfill_candidates_review_needed.json`

outputs:

- `data/reports/canonical_v2_llm_location_adjudication.json`
- `data/reports/canonical_v2_llm_location_adjudication.md`

## Goal

Classify the 81 `location_full` candidates semantically so obvious country-only
strings such as `Spain` or `Ukraine` are not written into `location_city`, while
city-state and clear city/country strings are isolated for later C3 application.

## Cost Arithmetic

This job uses the current Codex reasoning context to create a read-only
classification report. No external batch LLM subprocess is launched.

```
81 cids x semantic classification in current session
= negligible incremental pipeline tokens beyond this approved chat turn
projected weekly burn: <0.01% of 2B
```

## Guardrails

- No canonical artifact mutation.
- No Neon/R2 writes.
- No upload edits or execution.
- Report only.
- C3 apply still requires explicit user approval.

## Result

Command:

```bash
python3 tools/canonical_v2_llm_location_adjudication.py
```

Result:

- total location items: 81
- classified items: 81
- apply-city candidates: 38
- country-only strings not to write into city: 38
- classification kinds:
  - `city_country`: 9
  - `city_state`: 17
  - `city_region`: 8
  - `city_only_inferred_country`: 4
  - `country_only`: 38
  - other keep-review/locality types: 5
- writes: reports only

Reports:

- `data/reports/canonical_v2_llm_location_adjudication.json`
- `data/reports/canonical_v2_llm_location_adjudication.md`

Conclusion: LLM semantic classification recovered 38 `location_city` apply
candidates while correctly blocking obvious country-only strings such as
`Spain`, `Ukraine`, `Nigeria`, and `China` from being written to city.
