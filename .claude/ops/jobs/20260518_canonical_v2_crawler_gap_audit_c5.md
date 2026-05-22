# Job: canonical-v2-crawler-gap-audit-c5

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C5
status: complete

## Scope

write_scope: `tools/canonical_v2_crawler_gap_audit.py`,
`data/reports/canonical_v2_crawler_gap_audit.*`,
`.claude/ops/jobs/`, `.claude/ops/reviews/`, `.claude/Task.md`,
`.claude/REPORT.md`

input:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json`
- local source DBs in `data/crawl/`

outputs:

- `data/reports/canonical_v2_crawler_gap_audit.json`
- `data/reports/canonical_v2_crawler_gap_audit.md`
- `.claude/ops/reviews/20260518_crawler_gap_audit_c5.md`

## Goal

Determine whether remaining missing country/city/year fields are due to:

- canonical assembly missing structured source DB values;
- crawler/parser storing raw location text without structured parsing;
- year evidence in local source text; or
- true local source gaps that require re-crawl/external web search/null.

## Cost Arithmetic

No LLM batch work and no network.

```
39,776 rows x local SQLite/JSON audit
= 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only reports only.
- No source DB writes.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No `upload/` edits or execution.
- Produce a compact Claude Gate packet for later review.

## Result

completed: 2026-05-18 KST

- audit status: PASS
- writes: reports/review packet/logs only
- local structured source backfill candidates found: 0
- remaining source gaps:
  - `country_no_local_candidate`: 1,967
  - `city_no_local_candidate`: 2,010
  - `year_no_local_candidate`: 1,273
- local parser/semantic candidates:
  - `city_raw_location_candidate`: 2
  - `year_text_completion_signal_candidate`: 7
  - `year_text_noncompletion_candidate`: 24
- report:
  `data/reports/canonical_v2_crawler_gap_audit.md`
- Claude review packet:
  `.claude/ops/reviews/20260518_crawler_gap_audit_c5.md`
