# Job: canonical-v2-c6-web-search-smoke

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C6
status: complete

## Scope

write_scope: `data/reports/canonical_v2_c6_web_search_smoke.*`,
`.claude/ops/jobs/`, `.claude/Task.md`, `.claude/REPORT.md`

input:

- C4 missing location sample from
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json`
- web search results over 10 representative rows

outputs:

- `data/reports/canonical_v2_c6_web_search_smoke.json`
- `data/reports/canonical_v2_c6_web_search_smoke.md`

## Goal

Estimate whether remaining no-local-candidate metadata gaps can be solved via
targeted web search, and identify whether the process is safe for automation.

## Cost Arithmetic

Small manual smoke only.

```
10 sampled rows x web search + Codex adjudication
= negligible batch cost, no pipeline LLM/API batch
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only report only.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No source DB writes.
- No `upload/` edits or execution.

## Result

- smoke rows: 10
- likely safe apply after source review: 3
- partial evidence: 3
- unresolved: 2
- conflict/manual: 1
- keep-null policy: 1

Conclusion: targeted web search can recover some missing location metadata, but
bulk automatic web fill is unsafe without source ranking, exact-source matching,
and conflict handling.
