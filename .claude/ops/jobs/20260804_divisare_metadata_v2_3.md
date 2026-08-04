# Divisare metadata SQLite v2.3 review

## Objective

Close the remaining non-image Divisare metadata review work on top of the
immutable v2.2 SQLite artifact. Apply reviewed partial-text, area, and D2
identity decisions; rebuild all membership-dependent building surfaces; and
publish a new immutable v2.3 SQLite artifact.

## Scope

Included:

- Close the two remaining partial-text review rows as rejects
- Review all 125 ambiguous area rows
- Review all 220 pending/deferred Divisare-internal D2 pairs
- Rebuild redirects, active memberships, facets, core fields, article roles,
  and existing image URL/asset membership after approved identity merges
- Produce source-final v2.3 export and validation/report tables

Excluded:

- Image download, pHash computation, image classification, and Vision
- Vector/embedding generation
- Cross-site comparison or deduplication
- New credit collection
- Neon/R2 writes

External API/LLM/Vision/Neon/R2 cost: `$0`.

## Inputs

- `data/curated/divisare_metadata_v2_2.db`
  - SHA-256:
    `ee7bcd55fedf38fe8cb9a49f51e8f12f69493aef68ff1d201d2fa1e5be8ec95c`
  - size: `1,807,249,408 bytes`
- `canonical/divisare_partial_text_decisions_v2.json`
  - SHA-256:
    `de7e965e5a8127706fe3fba0067644fc096c2662b5319d86b5df2dfead5ae723`
- `canonical/divisare_area_decisions_v1.json`
  - SHA-256:
    `a8c73d0f83cc1c6fcd64782b74c579ff600cf1a541e03027341f4add650c7061`
- `canonical/divisare_d2_decisions_v1.json`
  - SHA-256:
    `dcc33813a31d8e0e1a3d452798cee15139180519f35b130346848ee2550f86a0`

All input hashes were unchanged after the build.

## Decisions

Partial text:

- 9 accept / 12 reject / 0 review
- articles `261731` and `261740` changed from review to reject as shared
  exhibition boilerplate

Area:

- 10 generic accepts
- 15 scope-qualified candidates
- 21 null/multi/conflict abstentions
- 79 non-area rejects
- 123 final / 2 open (`14079`, `465382`)
- no numeric value inferred from image content

D2:

- Identity scope: same architectural project/intervention
- Merge requires at least two independent evidence families and zero hard
  conflicts
- Candidate score, title, city/country, architect alone, and tag similarity do
  not authorize a merge
- Final 220: 8 merge / 128 reject / 84 defer
- Residence A/B, FASE I/III, and Vallecas 11/51 explicitly remain separate
- Long Museum and Rolex were downgraded from proposed merge to defer after an
  independent metadata-only audit found no article-specific second evidence
  family or exact shared asset/URL/membership

## Code and records

- `canonical/divisare_review_v23.py`
- `canonical/divisare_partial_text_decisions_v2.json`
- `canonical/divisare_area_decisions_v1.json`
- `canonical/divisare_d2_decisions_v1.json`
- `tools/build_divisare_reviewed_v23.py`
- `tests/test_divisare_v23_decisions.py`
- `tests/test_divisare_area_decisions_v23.py`
- `tests/test_divisare_d2_decisions_v23.py`
- `tests/test_divisare_reviewed_v23_builder.py`
- `tests/test_divisare_reviewed_v23.py`
- `docs/DIVISARE_D2_REVIEW_STATUS.md`
- `docs/DIVISARE_METADATA_REVIEW_V23.md`

## Smoke ladder

N=10 and N=100 production-like fixtures each ran the full build path twice.

- all output validations passed
- byte SHA and logical SHA matched between repeat builds
- parent/manifest SHA values were unchanged
- no-clobber preserved existing outputs
- N=10 exported 9 active buildings from 10 articles
- N=100 exported 99 active buildings from 100 articles

The component-safe union test also proved that a reject edge which would
collapse transitively through two merges aborts before publication.

## Full result

COMPLETE on 2026-08-04.

- Output: `data/curated/divisare_metadata_v2_3.db`
- Report: `data/reports/divisare_metadata_v2_3.md`
- schema: `PRAGMA user_version = 6`
- builder: `divisare-metadata-review-builder-v2.3.0`
- policy: `divisare-metadata-review-v2.3.0`
- size: `2,115,321,856 bytes`
- elapsed: `102.38 seconds`
- SHA-256:
  `7c263f430709dbfe4a2747407d736268331976cd77f66fe25d7037b77fd68038`
- logical SHA-256:
  `ce55d8c9d91005b0554ba9cb483661613d66c7e936e3edfb54bc0adf2c50e557`

Population:

- 29,955 articles / memberships
- 29,883 active buildings
- 8 terminal redirects / 117 related-project edges
- 74 confirmed / 128 rejected / 84 deferred D2 pairs
- 1,429 article-level generic areas
- 93,414 confirmed / 28,284 candidate facets
- 547,222 existing image asset memberships preserved/re-materialized

Validation:

- 86 full artifact checks passed / 0 failed
- SQLite quick/integrity check: `ok`
- foreign-key violations: 0
- all v2.2 parent user tables content-identical by typed logical SHA
- all v2.2 schema objects unchanged
- 304 unique D2 article guards matched
- Divisare suite: 138 passed

## Remaining work

Image processing remains intentionally unstarted:

- 547,222 `phash-256` rows are pending placeholders
- completed pHash values: 0
- image classifications: 0
- image match candidates: 0

Run image download/hash/semantic analysis only as a separate, versioned job
after accepting this metadata artifact. Use it to support deferred D2 and
cross-site duplicate review, never as the sole building identity signal.
