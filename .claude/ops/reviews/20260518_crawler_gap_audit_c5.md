# Claude Gate packet: canonical_v2 C5 crawler gap audit

created: 2026-05-18 KST
owner: DB-CODEX-OPS
stage: COMPLETENESS-C5
status: ready_for_review

## Scope

Read-only audit to determine whether remaining canonical_v2 metadata gaps
after C4 are due to local crawler/source data that was not propagated into
canonical, parser gaps, or true local-source absence.

No canonical artifact, Neon, R2, source DB, or upload mutation was performed.

## Inputs

- canonical input:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json`
- local source DBs:
  - `data/crawl/divisare.db`
  - `data/crawl/architizer.db`
  - `data/crawl/archello.db`
  - `data/crawl/metalocus.db`

## Outputs

- JSON report:
  `data/reports/canonical_v2_crawler_gap_audit.json`
- Markdown report:
  `data/reports/canonical_v2_crawler_gap_audit.md`
- job card:
  `.claude/ops/jobs/20260518_canonical_v2_crawler_gap_audit_c5.md`

## Result summary

| metric | count |
| --- | ---: |
| `country_no_local_candidate` | 1,967 |
| `city_no_local_candidate` | 2,010 |
| `city_raw_location_candidate` | 2 |
| `year_no_local_candidate` | 1,273 |
| `year_text_completion_signal_candidate` | 7 |
| `year_text_noncompletion_candidate` | 24 |

No `*_structured_source_available` metric appeared in the result. The audit
therefore found no local structured source field that canonical assembly had
obviously failed to carry forward for the remaining C4 gaps.

## Interpretation for review

- Most remaining gaps appear to be true local-source absences, not missed
  structured fields in the existing crawler DBs.
- `city_raw_location_candidate=2` suggests two rows may benefit from parser or
  source-specific review, not broad re-crawl.
- `year_text_completion_signal_candidate=7` suggests seven rows may have
  completion/opening/year evidence in local source text, but semantic review is
  required before applying.
- `year_text_noncompletion_candidate=24` contains years that are likely not safe
  project-year completions without stronger evidence.

## Claude review questions

1. Does the conclusion follow: C5 did not find a broad local crawler/canonical
   propagation bug for remaining gaps?
2. Should C5.1 manually review only the 9 high-value local candidates
   (`2` raw-location + `7` completion-signal rows)?
3. For the large `*_no_local_candidate` groups, should the sustainable process
   be: leave null by default, then use targeted recrawl/web search only when a
   source-specific freshness or product requirement justifies the cost?

## Proposed next gate

If Claude agrees, run a narrow C5.1 semantic review over the 9 local candidates
before considering any network crawling or web search.
