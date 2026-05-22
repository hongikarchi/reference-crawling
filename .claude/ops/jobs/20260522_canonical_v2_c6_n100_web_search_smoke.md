# Job: canonical-v2-c6-n100-web-search-smoke

created: 2026-05-22 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C6.2
status: complete

## Scope

write_scope:

- `data/reports/canonical_v2_c6_n100_web_search_smoke.json`
- `data/reports/canonical_v2_c6_n100_web_search_smoke.md`
- `.claude/ops/jobs/`
- `.claude/Task.md`
- `.claude/REPORT.md`

inputs:

- `data/reports/canonical_v2_c6_n100_smoke_queue.json`
- targeted web search over the deterministic N=100 queue

## Goal

Measure whether the remaining C6 metadata gaps can be recovered from web search
without mutating canonical artifacts or Neon.

## Cost Arithmetic

Manual web-search smoke only. No pipeline LLM/API batch and no DB writes.

```
100 queued rows x targeted web search/adjudication
= session interaction only, 0 pipeline LLM tokens
projected weekly burn: 0 / 2B = 0%
```

## Guardrails

- Read-only report generation only.
- No canonical artifact mutation.
- No Neon/R2 writes.
- No source DB writes.
- No `upload/` edits or execution.

## Result

- smoke queue size: 100
- apply candidates after exact-source review: 34
- partial candidates: 20
- conflict/manual candidates: 4
- policy-null/manual-null candidates: 9
- unresolved candidates: 33
- report:
  `data/reports/canonical_v2_c6_n100_web_search_smoke.md`

Conclusion: web search is useful enough to justify C6.3, but full automation is
not safe. Next step should build a source-ranked apply queue, not mutate all
search hits directly.
