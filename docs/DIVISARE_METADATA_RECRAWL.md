# Divisare Metadata HTML Recrawl

## Purpose

The immutable metadata v2.1 DB identifies 29,955 articles whose historical
description, area, or article-kind evidence may need a fresh DOM-aware read.
The recrawl is a separate enrichment sidecar. It does not rewrite
`data/curated/divisare_metadata_v2_1.db`.

Image semantics, image downloading, pHash, vectors, credits, cross-site
matching, Neon, and R2 are outside this stage.

## Storage model

Each crawl size has two independent outputs:

- A resumable SQLite state DB under `data/enrichment/`
- Gzip-compressed HTML snapshots under a sibling snapshot directory

The state DB records immutable parent lineage, queue priority/reasons, attempts,
HTTP status, final URL, content hash, snapshot path, parser version, parsed
metadata versions, and one current parse per article. Fetch status and parse
status are separate. A parser update can therefore reparse retained HTML
without another network request.

The crawler uses an authenticated Divisare session stored outside the artifact.
It rejects login walls, HTTP errors, and non-project responses instead of
accepting them as metadata.

Before offline parsing, the crawler verifies each decompressed snapshot against
its recorded byte size and SHA-256. Parser versions already present for a
snapshot are skipped, so chunked reparse is resumable and idempotent. An
existing state DB also rejects a different seed scope instead of silently
expanding an N=100 smoke into the full queue.

Current parsing policy: `divisare-html-metadata-v2.3`. The crawler policy is
`divisare-metadata-recrawl-v2.4.1`.

The combined Divisare metadata and DOM-parser regression suite contains 51
tests and passes in full. Sixteen of those tests exercise the v2.3 DOM parser
and v2.4.1 recrawl state contract.

## DOM parser policy

### Project identity

- The queued source URL must match `/projects/<article_id>-<slug>`.
- The page must contain exactly one `.project` root.
- The selected root's `data-project-id` must match the queued article ID.
- The final response URL's project ID must match the queued article ID.
- A mismatch is a parse failure, not a partial success.
- The response final URL is retained for audit, and redirects to login are
  rejected.

### Description

- Select `.project > .description`.
- Use only direct child paragraphs:
  `.description.find_all("p", recursive=False)`.
- Normalize whitespace, drop empty paragraphs, and preserve paragraph
  boundaries with a blank line.
- Do not include nested paragraphs from image cards, captions, credits,
  collection controls, or other media UI.
- If no direct prose paragraph exists, remove media/UI nodes and retain only a
  review-marked text fallback. A fallback is not treated as equal-confidence
  prose.
- Store the resulting prose hash, paragraph count, removed media-node count,
  and quality status so extraction changes are auditable.

### Area

Area is taken only from the project facts dimensions list:

- Locate a `.project_fact` row whose label is exactly `Built Surface`.
- Preserve the raw label, raw value, and DOM source.
- Ignore `Building Cubature` and area-like numbers mentioned only in prose.
- Divisare does not expose an explicit unit in the inspected `Built Surface`
  rows. Normalization to `area_sqm` therefore uses the documented source
  convention that `Built Surface` is square metres.
- Mark this as `implicit_square_metres_divisare`, with confidence `0.75`.
- Keep the raw value so this assumption can be revised without refetching.
- Treat dot/comma groups of three trailing digits as thousands separators
  where applicable.

### Other metadata

- Location and project year come from labeled project metadata, not free text.
- Article kind is confirmed only by an explicit DOM marker. Album tags,
  content hints, and title/slug lexical matches remain candidates in the
  immutable metadata DB.
- The inspected Divisare pages did not expose a reliable explicit article-kind
  marker, including pages associated with plans/details. A page-level plans
  tag must not be propagated to every gallery image.

## Smoke status

### N=10

The authenticated network smoke completed successfully:

- State DB:
  `data/enrichment/smoke/divisare_metadata_recrawl_n10_v2_1.db`
- Snapshot root:
  `data/enrichment/smoke/divisare_html_n10_v2_1`
- Accepted report:
  `data/reports/smoke/divisare_metadata_recrawl_n10_v2_3.md`
- Fetch: `10 success / 0 failed`
- Current v2.3 parse: `10 success / 0 failed`
- Description: `10 dom_prose_paragraphs`
- Built Surface: `9 / 10`
- Explicit article kind: `0`
- Name/country/city/year conflicts: `0`
- Project identity mismatch / prose fallback: `0 / 0`
- Metadata versions: `30` (`20` retained older parses + `10` current v2.3)
- Offline v2.3 reparse elapsed: `0.70s`
- Validation: integrity, foreign keys, current-row uniqueness, and snapshot
  linkage all passed
- API/LLM cost: `$0`

The initial network run used v2.1. The accepted current result is an offline
v2.3 reparse of the same ten retained snapshots; no refetch was needed.

### N=100

The authenticated v2.1 network run and v2.3 offline reparse both completed:

- State DB:
  `data/enrichment/smoke/divisare_metadata_recrawl_n100_v2_1.db`
- Snapshot root:
  `data/enrichment/smoke/divisare_html_n100_v2_1`
- Network report:
  `data/reports/smoke/divisare_metadata_recrawl_n100_v2_1.md`
- Accepted v2.3 report:
  `data/reports/smoke/divisare_metadata_recrawl_n100_v2_3.md`
- Network fetch: `100 success / 0 failed` in `299.11s`
- Current v2.3 parse: `100 success / 0 failed` in `7.03s`
- Description: `100 dom_prose_paragraphs`
- Built Surface: `63 / 100`
- Explicit article kind: `0`
- Name/country/city/year conflicts: `0`
- Project identity mismatch / prose fallback: `0 / 0`
- Snapshots / current metadata: `100 / 100`
- Metadata versions: `300` (`200` retained older parses + `100` current v2.3)
- Validation: integrity `ok`; all six recorded error counters are `0`
- A repeated v2.3 reparse with `--max-items 25` processed `0` rows in
  `0.02s`, confirming idempotent chunk resumption.
- API/LLM cost: `$0`

## Full-run status

The authenticated full crawl was approved and started on 2026-07-28:

- State DB: `data/enrichment/divisare_metadata_recrawl_v2_4.db`
- Snapshot root: `data/enrichment/divisare_html_snapshots_v2_4`
- Seed report: `data/reports/divisare_metadata_recrawl_v2_4_seed.md`
- Preflight report:
  `data/reports/divisare_metadata_recrawl_v2_4_preflight.md`
- Final report path:
  `data/reports/divisare_metadata_recrawl_v2_4.md`
- PID file: `data/enrichment/divisare_metadata_recrawl_v2_4.pid`
- Standard output/error:
  `logs/divisare_full_recrawl_v2_4_run3.stdout.log` and
  `logs/divisare_full_recrawl_v2_4_run3.stderr.log`
- Request delay: approximately three seconds
- Expected duration before retries: approximately 25 hours
- API/LLM cost: `$0`

The crawler holds an OS-level exclusive lock for the state DB. A login wall or
HTTP 401/403 triggers one credential-based automatic re-login when enabled,
without converting the remaining queue to blocked rows. Three consecutive
blocked responses or ten consecutive fetch/structural parse failures stop the
current run. Per-article commits allow the identical command to resume pending
rows after an interruption.

### Running checkpoint

Read-only snapshot at `2026-07-31 00:21:05 KST`:

- Queue: `29,955`
- Fetch: `11,635 success`, `18,308 pending`, `1 running`, `10 failed`,
  `1 not_found`, `0 blocked`
- Parse: `11,167 success`, `15 partial`, `453 no_content`, `0 failed`
- Snapshots / current metadata: `11,635 / 11,635`
- Current descriptions / areas: `11,182 / 952`
- Active run: `run_id=6`, `481` processed, no current error
- Remaining selectable jobs: `18,319`
- Estimated remaining time: approximately `15.3 hours`

The active command includes `--retry-terminal` and `--auto-relogin`. The ten
older failed jobs are ordered after pending work and will be retried. The
single confirmed 404 is intentionally excluded. Empty run3 stdout/stderr files
do not imply an idle process; progress is committed to the state DB and WAL.
Do not start a second crawler or remove the lock/PID files. The final report is
written only after normal completion and does not exist at this checkpoint.

Read-only progress inspection:

```powershell
.\.venv-divisare\Scripts\python.exe `
  tools\inspect_divisare_recrawl.py `
  --state-db data\enrichment\divisare_metadata_recrawl_v2_4.db
```
