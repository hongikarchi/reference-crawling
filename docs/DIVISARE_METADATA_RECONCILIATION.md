# Divisare Metadata Reconciliation v2.2

## Purpose

`divisare_metadata_v2_2.db` is the final Divisare-only, non-image-semantics
SQLite artifact. It reconciles the immutable metadata-v2.1 database with the
completed authenticated HTML recrawl while retaining both inputs unchanged.

This artifact is the Divisare source baseline to use before any cross-site
comparison. It does not contain vector embeddings or new image judgments.

## Inputs

- Metadata parent: `data/curated/divisare_metadata_v2_1.db`
  - schema: `PRAGMA user_version = 4`
  - SHA-256:
    `8186f49eac8199e0a5cfbd671c952169646b8829840ba9b8b6f85c2244b9deca`
- Recrawl sidecar: `data/enrichment/divisare_metadata_recrawl_v2_4.db`
  - schema: `PRAGMA user_version = 1`
  - SHA-256:
    `ea7865323fc0c861a7e5cba1a2ef6851683a3da86f57a445dd3b922301bd99b4`
  - crawler: `divisare-metadata-recrawl-v2.4.1`
  - parser: `divisare-html-metadata-v2.3`
- Partial-text decisions:
  `canonical/divisare_partial_text_decisions_v1.json`
  - version: `divisare-partial-text-review-v1.0`
  - SHA-256:
    `f31ee5b94afec2c5cde59f2479ea2e06a1e27925b24336f973b571e40838b5df`

The builder verifies both database hashes before and after the build. The
recrawl lineage must point to the exact supplied parent SHA.

## Output

- SQLite: `data/curated/divisare_metadata_v2_2.db`
- Report: `data/reports/divisare_metadata_v2_2.md`
- schema: `PRAGMA user_version = 5`
- metadata version: `divisare-metadata-v2.2`
- builder: `divisare-metadata-reconciliation-builder-v1.1`
- policy: `divisare-metadata-reconciliation-v1.1`
- size: `1,807,249,408 bytes`
- SHA-256:
  `ee7bcd55fedf38fe8cb9a49f51e8f12f69493aef68ff1d201d2fa1e5be8ec95c`
- reconciliation logical SHA-256:
  `fee79fc8185439a29053f4d94c00673d017c4effcfc52858a2343abe212ba942`

The parent database is copied with SQLite's backup API. Existing v2.1 tables
and views remain available in the output; v2.2 truth is stored in new overlay
tables and views.

## Resolution Policy

### Names and location

- Normalization-equivalent values confirm the parent display value.
- `South Korea` / `Korea, Republic of` and `Russia` / `Russian Federation`
  are aliases, not conflicts.
- A true parent/recrawl conflict preserves the parent value and enters review.
- Delimiter artifacts such as `-`, `- Nis`, and `France -` are cleaned or
  rejected instead of becoming country/city values.
- Five historical names that were only `city - country` placeholders are
  null in v2.2 and explicitly review-marked.

### Project year

- Years must be integers in `1000..2100`.
- Six historical values parsed as `1`, `9`, or `12` are not retained as
  project years. The raw parent evidence remains in the v2.1 tables.
- Valid recrawl values fill missing parent values; true conflicts preserve the
  parent value and enter review.

### Built Surface

The legacy recrawl `area_sqm` value is not trusted directly. v2.2 reparses the
retained `recrawl_area_raw` with `divisare-metadata-reconciliation-v1.1`.

- Explicit square-metre aliases and locale-specific separators are supported.
- Square feet are converted with `1 ft2 = 0.09290304 m2`.
- Consistent dual-unit values use the metric side.
- Exact two-factor square-area expressions are computed.
- Hectares and large QA outliers retain a candidate value but are not
  published automatically.
- Cubic, linear, additive, range, and ambiguous-unit values are quarantined.
- Scope-qualified values such as `roof area`, `footprint`, `useful area`, and
  `aboveground` do not populate generic `area_sqm`. A unique parsed hypothesis
  remains in `area_candidate_sqm` for review.
- Safe qualifiers such as `gross`, `GFA`, `built surface`, and approximation
  labels remain eligible. Residual numbers and labels must be classified;
  otherwise automatic publication fails a validation gate.

Of 1,544 raw Built Surface values, 1,419 are automatically usable and 125 are
review items. The automatic set contains 1,024 explicit metric, 370 implicit
metric, 11 dual-unit, 7 imperial-converted, 4 reparsed, and 3 multiplier rows.
Thirteen review rows retain a single numeric candidate: nine scoped values,
one unknown-unit suffix, one hectare, and two QA outliers. Additive expressions
with no unique total retain their components only in evidence JSON.

### Description

- `dom_prose_paragraphs`: 27,602 rows are accepted.
- `dom_text_fallback_review`: all 21 rows received a hash-guarded semantic
  decision: 9 accept, 10 reject, 2 remain review.
- `no_prose_content`: 2,322 valid image-only/no-prose pages remain null. The
  historical flattened text is retained for audit but is not republished as
  prose because it may consist of captions or credits.
- `not_found`: 10 source tombstones retain the historical parent description
  and metadata with explicit unavailable/review status.

The final publishable article-description count is 27,621.

## Tables and Views

New v2.2 tables:

- `metadata_reconciliation_lineage_v2_2`: exact input, policy, decision, and
  scope lineage.
- `article_recrawl_evidence_v2_2`: copied current recrawl evidence, snapshot
  hash/path, raw fields, parser version, and status.
- `article_partial_text_decisions_v2_2`: applied partial-text decisions and
  content-hash guards. Missing and extra decision IDs both abort the build.
- `article_metadata_resolution_v2_2`: resolved scalar values, per-field source
  and status, explicit area candidate/evidence columns, conflicts, and review
  reasons.
- `building_core_reconciled_v2_2`: building-level core consensus over the
  existing active memberships.
- `metadata_reconciliation_metrics_v2_2` and
  `metadata_reconciliation_validation_v2_2`: build accounting and gates.

Primary views:

- `v_article_metadata_reconciled_v2_2`: article truth plus candidate and
  historical descriptions.
- `v_divisare_buildings_export_v2_2`: source-final building export.
- `v_divisare_metadata_review_v2_2`: article review queue.
- `v_divisare_recrawl_status_v2_2`: recrawl and resolution status.
- `v_metadata_d2_review_v2_2`: all 286 existing D2 pairs with current metadata
  evidence for both sides.

The building export adds complete `confirmed_facets_json` and
`candidate_facets_json` arrays. Compatibility scalar/array fields from v2.1
remain available.

## Preserved State

- Articles: 29,955
- Active buildings: 29,891
- Source tags / article-tag occurrences: 680 / 113,326
- Attribute claims / claim evidence: 282,486 / 282,486
- Confirmed / candidate building facets: 93,425 / 28,285
- D2: 66 confirmed / 220 pending / 0 redirects
- Image occurrences / URLs: 577,112 / 577,112
- Image assets / materialized building images: 547,222 / 547,222
- Image classifications: 0

Cover and gallery URLs are exactly equal between the v2.1 and v2.2 export for
every active building. No image download, pHash, or image-semantic operation
is performed by this builder. Typed logical hashes also prove exact content
preservation for all 47 parent user tables, rather than comparing row counts
alone.

## Coverage and Review

Article coverage:

- name: 29,950
- country: 29,930
- city: 29,926
- year: 29,949
- area: 1,419
- publishable description: 27,621
- reconciliation review: 154

Active-building coverage:

- name: 29,886
- country: 29,866
- city: 29,864
- year: 29,885
- area: 1,419
- metadata/reconciliation review: 2,910

The 2,910 building review rows include inherited v2.1 D2/facet/core review
state; they are not all newly introduced recrawl conflicts.

## Validation

- N=10: 43 checks passed; 10 rows inspected individually.
- N=100: 43 checks passed; byte and logical determinism passed on a repeated
  build. Two ambiguous multi-value areas were correctly quarantined.
- Full: 43 checks passed, SQLite quick/integrity check `ok`, foreign-key
  violations `0`.
- Policy tests: 47 passed.
- Builder integration tests: 7 passed.
- Complete Divisare regression suite: 105 passed.

The integration fixture covers direct prose, no-content suppression, 404
tombstones, robust area parsing, country aliases, partial accept/reject/hash
guards and exact decision sets, parent-table content hashes, residual-area
gates, taxonomy JSON, image URL equality, and no-clobber behavior.

## Rebuild

```powershell
.\.venv-divisare\Scripts\python.exe `
  tools\build_divisare_reconciled.py
```

Outputs are immutable. A changed parser, policy, or decision file requires a
new versioned artifact path.

## Remaining Work

- Resolve or retain the two partial-text review rows based on a human product
  decision.
- Review the 125 area candidates/quarantines only if additional coverage is
  worth the manual effort.
- Keep the 220 D2 pairs separate until versioned human decisions are supplied.
- Image pHash, duplicate-image work, image semantics, vectors, and cross-site
  comparison are later stages and are intentionally absent here.
